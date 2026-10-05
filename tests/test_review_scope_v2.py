"""AC.1-AC.3 review-only v2 scope / new-cell inventory / Southwest packet tests.

Nearest existing tests and the plausible missed fault they would NOT catch:

- Nearest test for scope/inspection:
  ``tests/test_trial_guidance.py::test_inventory_covers_all_pinned_detailed_refs_with_hashed_evidence``
  asserts the trial_guidance inspector covers exactly the 67 detailed-contract
  allowlist references.
- Nearest test for Southwest:
  ``tests/test_trial_southwest.py::test_southwest_all_67_accounted_no_model_or_workbook``
  asserts the v1 Southwest packet covers exactly 67 references.

Plausible missed fault: neither existing test covers the ten workbook cells that
are outside the 67-reference allowlist — ``Input sheet!B9``, ``C11``..``C15``,
``C19``..``C21`` (the "Last 10K before LTM" prior-fiscal-year column) and ``D11``
(the years-since-last-10K fraction). A regression that (a) dropped any of those
ten cells from scope, (b) accidentally pulled a deferred B>=45 reference or
``Cost of capital worksheet!H32`` into scope, (c) let the checked-in v2
inventory JSON drift from the pinned workbook, or (d) invented a prior-period
value in the Southwest packet would pass the existing suite. AC.1-AC.3 below are
defined with independently derived expected reference sets and negative
period/absence/ID cases so those faults fail here.
"""
from __future__ import annotations

import hashlib
import json
import re
import shutil
import zipfile
import xml.etree.ElementTree as ET
from copy import deepcopy
from pathlib import Path

import pytest

from airline_valuation.review_scope_v2 import (
    CONTRACT,
    DEFERRED_REFS,
    EXCLUDED_H32,
    NEW_CELL_REFS,
    NEW_CELL_REFS_SET,
    PINNED_WORKBOOK_SHA256,
    RETAINED_B_LT45_REFS,
    RETAINED_COST_REFS,
    RETAINED_V1_REFS,
    SCOPE_REFS,
    ReviewScopeError,
    _canonical_digest,
    _json_sha,
    build_new_cell_inventory,
    build_southwest_review,
    inspect_new_cells,
    load_input_policy,
    load_new_cell_inventory,
    load_review_scope,
    load_southwest_review,
    new_cell_by_reference,
    validate_approved_baselines,
    validate_cross_boundary_routes,
    validate_fixed_allowlist,
    validate_input_policy_entries,
    validate_new_cell_entries,
    validate_southwest_conclusions,
)
from airline_valuation.trial_guidance import inspect_trial_guidance

ROOT = Path(__file__).resolve().parents[1]
WORKBOOK = ROOT / "fcffsimpleginzu.xlsx"
DOCS = ROOT / "docs" / "policy-drafts"
V1_POLICY = DOCS / "ginzu-input-policy-v1.json"
NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}


# Fixed SHA-256 of the approved existing source files, captured as observed
# digests at remediation time from the approved files themselves (the approved
# plan does NOT supply these hashes; recording them is independent provenance,
# not a plan-supplied pin). Any drift of these files makes the v2 review
# invalid and the baseline tests below must reject it.
_APPROVED_BASELINES = {
    "src/airline_valuation/detailed_contract.json": "733aa532dffb89a5e4142bdc6851ec3bd255c0684bc5197a937d864d2c95fc8e",
    "docs/policy-drafts/ginzu-input-policy-v1.json": "bb793ec27a0818a7b83a1aaa89f139627a82b729b4325b1ee8e88e9889c47622",
    "docs/ginzu-input-policy-v1.md": "51c558c7428f1021e91473e549ac3e494a417a1028a717971c83b68c9c03cdb0",
    "docs/policy-drafts/ginzu-review-manifest-v1.json": "b43fd0852f323ec00ae298d3ca59e5121d10cfa98184450f51f7e091e832e10c",
    "docs/ginzu-policy-review-packet-v1.md": "c819458fed25d3e9f562a0ac376d3c079ba45269e7688d7a272432f30795ab15",
    "docs/southwest-input-review.md": "b1e66e7b9d075fb03e172f2bd85da7007bb41bf8ce7577f9f5a1ff7262377a39",
    "review-artifacts/ginzu-policy-proposed-review-only-manifest.json": "fd5000675ba90bb7a57559580404ce70a1706889e8c464568fae38fc70d756cc",
}

# The 67 detailed-contract input references enumerated independently (copied
# verbatim from detailed_contract.json input_allowlist / ginzu-input-policy-v1.json
# as of the recorded baseline hashes; fixed literal, not derived from the live
# files) followed by the ten new cells.
_EXPECTED_ALLOWLIST = [
    "Input sheet!B8", "Input sheet!B11", "Input sheet!B12", "Input sheet!B13",
    "Input sheet!B14", "Input sheet!B15", "Input sheet!B16", "Input sheet!B17",
    "Input sheet!B18", "Input sheet!B19", "Input sheet!B20", "Input sheet!B21",
    "Input sheet!B22", "Input sheet!B23", "Input sheet!B24", "Input sheet!B26",
    "Input sheet!B27", "Input sheet!B28", "Input sheet!B29", "Input sheet!B30",
    "Input sheet!B31", "Input sheet!B32", "Input sheet!B34", "Input sheet!B37",
    "Input sheet!B38", "Input sheet!B39", "Input sheet!B40", "Input sheet!B41",
    "Input sheet!B45", "Input sheet!B48", "Input sheet!B49", "Input sheet!B51",
    "Input sheet!B52", "Input sheet!B53", "Input sheet!B54", "Input sheet!B56",
    "Input sheet!B57", "Input sheet!B59", "Input sheet!B61", "Input sheet!B62",
    "Input sheet!B64", "Input sheet!B65", "Input sheet!B67", "Input sheet!B68",
    "Input sheet!B70", "Input sheet!B71", "Input sheet!B72",
    "Cost of capital worksheet!B11", "Cost of capital worksheet!B21",
    "Cost of capital worksheet!B25", "Cost of capital worksheet!B32",
    "Cost of capital worksheet!B33", "Cost of capital worksheet!B35",
    "Cost of capital worksheet!B48", "Cost of capital worksheet!B49",
    "Cost of capital worksheet!B50",
    "Cost of capital worksheet!H21", "Cost of capital worksheet!H22",
    "Cost of capital worksheet!H23", "Cost of capital worksheet!H24",
    "Cost of capital worksheet!H25", "Cost of capital worksheet!H26",
    "Cost of capital worksheet!H27", "Cost of capital worksheet!H28",
    "Cost of capital worksheet!H29", "Cost of capital worksheet!H30",
    "Cost of capital worksheet!H31",
]
_EXPECTED_NEW_CELLS = [
    "Input sheet!B9", "Input sheet!C11", "Input sheet!C12", "Input sheet!C13",
    "Input sheet!C14", "Input sheet!C15", "Input sheet!C19", "Input sheet!C20",
    "Input sheet!C21", "Input sheet!D11",
]


def _row(ref: str) -> int:
    return int(re.search(r"[0-9]+$", ref).group())


def _independent_expected_sets():
    """Derive the expected v2 groups from the FIXED enumeration, not the live files."""
    allowlist = _EXPECTED_ALLOWLIST
    b_lt45 = [r for r in allowlist if r.startswith("Input sheet!") and _row(r) < 45]
    b_ge45 = [r for r in allowlist if r.startswith("Input sheet!") and _row(r) >= 45]
    cost = [r for r in allowlist if r.startswith("Cost of capital worksheet!")]
    new = list(_EXPECTED_NEW_CELLS)
    return b_lt45, b_ge45, cost, new


def _independent_b9_digest() -> str:
    """Independently compute SHA256(ET.tostring(B9)) straight from the zip bytes."""
    with zipfile.ZipFile(WORKBOOK, "r") as archive:
        root = ET.fromstring(archive.read("xl/worksheets/sheet1.xml"))
        cell = next(c for c in root.findall(".//m:sheetData/m:row/m:c", NS)
                    if c.attrib.get("r") == "B9")
        # ET.tostring output depends on the process-global namespace registry; use
        # the same pristine canonical serialization the module is pinned against.
        return _canonical_digest(cell)


# ---------------------------------------------------------------------------
# AC.1 — scope is ordered, unique, 58; deferred exact 19; H32 excluded
# ---------------------------------------------------------------------------


def test_ac1_scope_ordered_unique_58_with_independent_sets_and_exact_deferred():
    b_lt45, b_ge45, cost, new = _independent_expected_sets()
    # The contract splits exactly as approved-plan: 28 B<45, 19 B>=45, 20 cost.
    assert len(b_lt45) == 28 and len(b_ge45) == 19 and len(cost) == 20 and len(new) == 10
    assert len(new) == len(set(new)) == 10
    assert new == NEW_CELL_REFS
    assert NEW_CELL_REFS_SET == frozenset(new)

    # The scope is exactly the independently derived 58 and is ordered/unique.
    expected_scope = b_lt45 + cost + new
    assert SCOPE_REFS == expected_scope
    assert len(SCOPE_REFS) == len(set(SCOPE_REFS)) == 58
    assert RETAINED_B_LT45_REFS == b_lt45
    assert RETAINED_COST_REFS == cost
    assert RETAINED_V1_REFS == b_lt45 + cost

    # Deferred set is exactly the 19 B>=45 Input references.
    assert DEFERRED_REFS == b_ge45
    assert len(DEFERRED_REFS) == 19
    # Every allowlist reference is either in scope or deferred; the boundary is row 45.
    assert set(CONTRACT["input_allowlist"]) == set(SCOPE_REFS[:48]) | set(DEFERRED_REFS)
    assert all(_row(r) < 45 for r in b_lt45)
    assert all(_row(r) >= 45 for r in b_ge45)

    # H32 is excluded and absent from the allowlist.
    assert EXCLUDED_H32 == "Cost of capital worksheet!H32"
    assert EXCLUDED_H32 not in CONTRACT["input_allowlist"]
    assert EXCLUDED_H32 not in SCOPE_REFS

    # Negative: a deferred reference and H32 must never be in scope.
    assert "Input sheet!B45" not in SCOPE_REFS
    assert "Input sheet!B72" not in SCOPE_REFS
    assert EXCLUDED_H32 not in SCOPE_REFS

    # The checked-in review-scope mapping is consistent with the module.
    scope_json = load_review_scope()
    mapping = scope_json["mapping"]
    assert [item["reference"] for item in mapping] == SCOPE_REFS
    assert [item["position"] for item in mapping] == list(range(1, 59))
    assert sum(1 for item in mapping if item["entry_status"] == "retained_from_v1") == 48
    assert sum(1 for item in mapping if item["entry_status"] == "new_cell") == 10
    assert all((item["v1_entry_id"] is not None) == (item["entry_status"] == "retained_from_v1")
               for item in mapping)

    # Retained entries must preserve the v1 proposals verbatim and pin them.
    v1 = json.loads(V1_POLICY.read_text(encoding="utf-8"))
    v1_by_ref = {e["reference"]: e for e in v1["entries"]}
    policy = load_input_policy()
    retained = [e for e in policy["entries"] if e["entry_status"] == "retained_from_v1"]
    assert len(retained) == 48
    for entry in retained:
        v1_entry = v1_by_ref[entry["reference"]]
        assert entry["proposal"] == v1_entry["proposal"], entry["reference"]
        assert entry["v1_entry_id"] == v1_entry["id"]
        assert entry["retention"]["v1_proposal_sha256"] == _json_sha(v1_entry["proposal"])
    new_entries = [e for e in policy["entries"] if e["entry_status"] == "new_cell"]
    assert len(new_entries) == 10
    assert {e["reference"] for e in new_entries} == set(new)


def test_ac1_approved_baselines_pinned_fixed_refs_and_mutation_rejection(tmp_path: Path):
    # The fixed expected membership is enumerated independently above and must
    # still match the operational contract's allowlist.
    validate_fixed_allowlist(_EXPECTED_ALLOWLIST)
    assert CONTRACT["input_allowlist"] == _EXPECTED_ALLOWLIST

    # The approved source files must hash to their fixed recorded baselines.
    observed = validate_approved_baselines(_APPROVED_BASELINES)
    assert observed == _APPROVED_BASELINES

    # Mutation rejection: a drifted expected baseline fails the validator.
    with pytest.raises(ReviewScopeError, match="approved baseline drifted"):
        validate_approved_baselines({** _APPROVED_BASELINES,
                                     "docs/policy-drafts/ginzu-input-policy-v1.json": "0" * 64})

    # Mutation rejection: a real file whose bytes are mutated fails the validator.
    mutated = tmp_path / "ginzu-input-policy-v1-mutated.json"
    mutated.write_bytes(V1_POLICY.read_bytes() + b" ")
    with pytest.raises(ReviewScopeError, match="approved baseline drifted"):
        validate_approved_baselines({str(mutated): _APPROVED_BASELINES["docs/policy-drafts/ginzu-input-policy-v1.json"]})
    # A mutated file with its own correct (new) hash is not an approved baseline.
    validate_approved_baselines({str(mutated): hashlib.sha256(mutated.read_bytes()).hexdigest()})
    assert hashlib.sha256(mutated.read_bytes()).hexdigest() != _APPROVED_BASELINES["docs/policy-drafts/ginzu-input-policy-v1.json"]

    # The v2 input-policy validator (not the builder) accepts the real entries
    # and rejects a mutated v1 (retained proposal drift).
    policy = load_input_policy()
    validate_input_policy_entries(policy["entries"])
    drifted = deepcopy(policy)
    entry = next(e for e in drifted["entries"] if e["reference"] == "Input sheet!B8")
    entry["proposal"]["source_concept"] = entry["proposal"]["source_concept"] + " [mutated]"
    with pytest.raises(ReviewScopeError, match="not preserved verbatim"):
        validate_input_policy_entries(drifted["entries"])


# ---------------------------------------------------------------------------
# AC.2 — independent new-cell inventory: exactly ten, correct digests, negatives
# ---------------------------------------------------------------------------


def test_ac2_new_cell_inventory_exact_ten_and_negative_cases():
    b_lt45, b_ge45, cost, new = _independent_expected_sets()

    inventory = inspect_new_cells(WORKBOOK)
    assert inventory["source_matches_pin"] is True
    assert inventory["observed_source_sha256"] == PINNED_WORKBOOK_SHA256 == CONTRACT["sha256"]
    assert inventory["reference_count"] == 10
    assert [entry["reference"] for entry in inventory["entries"]] == NEW_CELL_REFS
    assert [entry["reference"] for entry in inventory["entries"]] == new

    # Canonical serialization: cell digests are SHA256(ET.tostring(cell)); all distinct.
    digests = {entry["cell_xml_sha256"] for entry in inventory["entries"]}
    assert len(digests) == 10
    assert all(len(d) == 64 for d in digests)
    assert all(entry["cell_present"] for entry in inventory["entries"])
    assert all(len(entry["worksheet_xml_sha256"]) == 64 for entry in inventory["entries"])

    # Cross-domain check: the Input-sheet worksheet digest matches trial_guidance.
    trial_by_ref = {e["reference"]: e for e in inspect_trial_guidance(WORKBOOK)["entries"]}
    input_ws = trial_by_ref["Input sheet!B8"]["worksheet_xml_sha256"]
    assert all(entry["worksheet_xml_sha256"] == input_ws for entry in inventory["entries"])

    # Independent B9 digest derived from the raw zip matches the inspector.
    assert inventory["entries"][0]["reference"] == "Input sheet!B9"
    assert inventory["entries"][0]["cell_xml_sha256"] == _independent_b9_digest()

    # B9 keeps its original validation and comment records; C13 has none (honest absence).
    b9 = inventory["entries"][0]
    assert any(v["type"] == "list" and v["sqref"] == "B8:B9" and "'Industry Averages(US)'!$A$2:$A$95" in (v["formula1"] or "")
               for v in b9["validations"])
    assert b9["comment_status"] == "present" and len(b9["comments"]) == 2
    c13 = next(e for e in inventory["entries"] if e["reference"] == "Input sheet!C13")
    assert c13["comment_status"] == "absent in related comment parts" and c13["comments"] == []

    # Checked-in inventory JSON must not drift from the pinned workbook.
    assert load_new_cell_inventory() == build_new_cell_inventory()

    # Robustness against the plausible serialization fault: ET.tostring digests
    # depend on the process-global namespace-prefix registry, which libraries
    # such as openpyxl (imported by other test modules at collection time) mutate.
    # The inspector must keep returning the pinned digests under that mutation.
    ET.register_namespace("m", "http://schemas.openxmlformats.org/spreadsheetml/2006/main")
    ET.register_namespace("r", "http://schemas.openxmlformats.org/officeDocument/2006/relationships")
    try:
        assert inspect_new_cells(WORKBOOK)["entries"] == load_new_cell_inventory()["entries"]
    finally:
        for uri in ("http://schemas.openxmlformats.org/spreadsheetml/2006/main",
                    "http://schemas.openxmlformats.org/officeDocument/2006/relationships"):
            ET._namespace_map.pop(uri, None)
    assert inspect_new_cells(WORKBOOK)["entries"] == load_new_cell_inventory()["entries"]

    # Negative absence: the inspector fails closed on a non-pinned workbook.
    changed = ROOT / "fcffsimpleginzu-changed-tmp.xlsx"
    shutil.copyfile(WORKBOOK, changed)
    try:
        changed.write_bytes(changed.read_bytes() + b"x")
        with pytest.raises(ReviewScopeError, match="does not match pinned"):
            inspect_new_cells(changed)
    finally:
        changed.unlink(missing_ok=True)

    # Negative period / absence / ID: anything outside the ten new cells is rejected.
    for bad_ref in (
        "Input sheet!B8",        # retained v1 cell, not a new cell
        "Input sheet!B10",       # same period family, not in the ten
        "Input sheet!C22",       # absent row in the prior-10-K column
        "Input sheet!D12",       # same 'years since last 10K' column, not in the ten
        "Cost of capital worksheet!H32",  # excluded
    ):
        with pytest.raises(ReviewScopeError):
            new_cell_by_reference(bad_ref)
    assert new_cell_by_reference("Input sheet!D11")["reference"] == "Input sheet!D11"

    # The ten new cells are outside the 67-reference allowlist entirely.
    assert not (set(NEW_CELL_REFS) & set(CONTRACT["input_allowlist"]))


def test_ac2_validator_rejects_swapped_period_zero_from_absence_and_mismatched_ids():
    policy = load_input_policy()
    # The independently specified validator (not the builder as sole oracle)
    # accepts the real 58 entries.
    validate_input_policy_entries(policy["entries"])
    new_entries = [e for e in policy["entries"] if e["entry_status"] == "new_cell"]
    validate_new_cell_entries(new_entries)

    # Swapped prior period: a C-column cell claims the LTM/FY2025 period.
    mutated = deepcopy(policy)
    c11 = next(e for e in mutated["entries"] if e["reference"] == "Input sheet!C11")
    c11["classification"]["period"] = "fiscal year; LTM/FY2025"
    with pytest.raises(ReviewScopeError, match="does not state a prior period"):
        validate_input_policy_entries(mutated["entries"])

    # Swapped prior period: source_concept presents FY2025 as the C-column value.
    mutated = deepcopy(policy)
    c14 = next(e for e in mutated["entries"] if e["reference"] == "Input sheet!C14")
    c14["proposal"]["source_concept"] = "FY2025 book value of equity"  # no 'never'
    with pytest.raises(ReviewScopeError, match="presents FY2025 as the C-column value"):
        validate_input_policy_entries(mutated["entries"])

    # Zero-from-absence: a cell with no comment/evidence must abstain, never zero.
    for ref in ("Input sheet!C13", "Input sheet!C20"):
        mutated = deepcopy(policy)
        entry = next(e for e in mutated["entries"] if e["reference"] == ref)
        entry["proposal"]["missing_component"] = "assume zero"
        with pytest.raises(ReviewScopeError, match="assumes zero from absence"):
            validate_input_policy_entries(mutated["entries"])

    # Mismatched IDs: evidence_link/inspector cell digest disagreement.
    mutated = deepcopy(policy)
    d11 = next(e for e in mutated["entries"] if e["reference"] == "Input sheet!D11")
    d11["proposal"]["evidence_link"]["cell_xml_sha256"] = "0" * 64
    with pytest.raises(ReviewScopeError, match="cell_xml mismatch"):
        validate_input_policy_entries(mutated["entries"])

    # Mismatched IDs: retained v1_entry_id swapped to a different entry.
    mutated = deepcopy(policy)
    b11 = next(e for e in mutated["entries"] if e["reference"] == "Input sheet!B11")
    b11["v1_entry_id"] = "GINZU-999"
    with pytest.raises(ReviewScopeError, match="v1_entry_id mismatch"):
        validate_input_policy_entries(mutated["entries"])


# ---------------------------------------------------------------------------
# AC.3 — Southwest packet: 58 honest rows, established locators only
# ---------------------------------------------------------------------------


def test_ac3_southwest_58_rows_honest_statuses_and_established_locators_only():
    b_lt45, b_ge45, cost, new = _independent_expected_sets()

    sw = load_southwest_review()
    assert sw["schema_version"] == "ginzu-southwest-review/2"
    assert sw["row_count"] == 58
    assert [row["reference"] for row in sw["rows"]] == SCOPE_REFS
    assert len(sw["rows"]) == len({row["reference"] for row in sw["rows"]}) == 58
    assert [row["position"] for row in sw["rows"]] == list(range(1, 59))

    # Checked-in Southwest JSON must not drift from the module builder.
    assert sw == build_southwest_review()

    # Every row is non-approving and non-writing.
    assert all(row["decision"] is None and row["write_eligible"] is False for row in sw["rows"])

    # Only B11/B12/B14 carry established candidate values; locators are non-empty only there.
    established_values = {
        row["reference"]: row["candidate_value_millions_usd"]
        for row in sw["rows"] if row["candidate_value_millions_usd"] is not None
    }
    assert established_values == {
        "Input sheet!B11": "28063",
        "Input sheet!B12": "428",
        "Input sheet!B14": "7981",
    }
    for row in sw["rows"]:
        if row["reference"] in established_values:
            assert row["established_locators"], row["reference"]
        else:
            assert row["candidate_value_millions_usd"] is None

    # B15/B17 are honest abstentions with established locators but no selected value.
    by_ref = {row["reference"]: row for row in sw["rows"]}
    assert by_ref["Input sheet!B15"]["status"] == "abstain_debt_presentation_unresolved"
    assert by_ref["Input sheet!B17"]["status"] == "abstain_lease_overlap_unresolved"
    assert by_ref["Input sheet!B15"]["candidate_value_millions_usd"] is None
    assert by_ref["Input sheet!B17"]["candidate_value_millions_usd"] is None
    assert by_ref["Input sheet!B15"]["established_locators"]  # the 4,901/4,919/4,929 paths

    # All statuses are from the honest review vocabulary.
    allowed = {
        "source_candidate_pending_rights_cutoff_independent_review",
        "abstain_debt_presentation_unresolved",
        "abstain_lease_overlap_unresolved",
        "abstain_out_of_bounded_trial_scope",
        "abstain_no_established_locator",
        "abstain_no_established_locator_prior_period_column",
    }
    assert {row["status"] for row in sw["rows"]} <= allowed

    # The ten new cells abstain honestly: no locator, no value, no FY2025 B copy.
    for ref in new:
        row = by_ref[ref]
        assert row["candidate_value_millions_usd"] is None
        assert row["established_locators"] == []
        assert row["status"].startswith("abstain_no_established_locator")
    for ref in ("Input sheet!C11", "Input sheet!C12", "Input sheet!C13",
                "Input sheet!C14", "Input sheet!C15", "Input sheet!C19",
                "Input sheet!C20", "Input sheet!C21"):
        assert by_ref[ref]["status"] == "abstain_no_established_locator_prior_period_column"

    # Negative: no invented value, no approval/signoff field, no private bytes leak.
    raw = (DOCS / "ginzu-southwest-review-v2.json").read_text(encoding="utf-8")
    assert "99999" not in raw
    assert '"approval"' not in raw and '"signoff"' not in raw  # no approval field
    assert "luv-20251231" not in raw  # primary document content is not reproduced
    assert all("prior-10-K" in by_ref[r]["reason"] for r in ("Input sheet!C11", "Input sheet!C15"))

    # Machine-readable cross-boundary facts (corrected): the lease-converter path
    # is IN SCOPE and reviewable (Cost B45/C55/C60 and Valuation output B5/B25/B39
    # feed from Input B17), while the deferred Input B45+ terminal policy is a
    # COVERAGE GAP with no demonstrated direct dependency from B17/B15/C15.
    scope_json = load_review_scope()
    routes = {r["id"]: r for r in scope_json["cross_boundary_routes"]}
    lease = routes["LEASE_CONVERTER_FEEDS_IN_SCOPE_COST_B45"]
    assert lease["conclusion_status"] == "reviewable_in_scope"
    assert lease["blocked_by_deferral"] is False
    assert "Input sheet!B17" in lease["scope_trigger_refs"]
    feed_refs = {item["reference"] for item in lease["in_scope_feed_path"]}
    assert {"Cost of capital worksheet!B45", "Cost of capital worksheet!C55",
            "Cost of capital worksheet!C60", "Valuation output!B5",
            "Valuation output!B25", "Valuation output!B39"} <= feed_refs
    coverage = routes["TERMINAL_REVIEW_COVERAGE_DEFERRED_INPUT_B45_PLUS"]
    assert coverage["conclusion_status"] == "coverage_gap"
    assert coverage["blocked_by_deferral"] is False
    assert coverage["deferred_refs"] == DEFERRED_REFS  # exact 19
    validate_cross_boundary_routes(scope_json["cross_boundary_routes"])

    # Southwest packet: B15/B17 are unresolved accounting abstentions (NOT
    # blocked by deferral); the in-scope lease path and the deferred terminal
    # coverage gap are machine-readable.
    assert sw["in_scope_lease_path"][0]["id"] == "LEASE_CONVERTER_FEEDS_IN_SCOPE_COST_B45"
    assert sw["coverage_gaps"][0]["id"] == "TERMINAL_REVIEW_COVERAGE_DEFERRED_INPUT_B45_PLUS"
    for ref in ("Input sheet!B15", "Input sheet!B17", "Input sheet!C15"):
        assert by_ref[ref]["conclusion_status"] == "unresolved", ref
    assert "coverage" in sw["conclusion_gate"].casefold()
    validate_southwest_conclusions(sw["rows"])

    # Cost references and the remaining B<45 references are out of bounded scope.
    for ref in cost:
        assert by_ref[ref]["status"] == "abstain_out_of_bounded_trial_scope"
    assert len([r for r in sw["rows"] if r["status"] == "abstain_out_of_bounded_trial_scope"]) == (
        58 - 5 - 10
    )


def test_ac3_no_false_blocked_by_deferral_and_coverage_gap_validation():
    sw = load_southwest_review()
    validate_southwest_conclusions(sw["rows"])
    scope_json = load_review_scope()
    validate_cross_boundary_routes(scope_json["cross_boundary_routes"])

    # No cell-level row may be marked blocked by deferral: the earlier false
    # "first-deferred linkage / all-19-crosses" framing is rejected.
    mutated = deepcopy(sw)
    b17 = next(r for r in mutated["rows"] if r["reference"] == "Input sheet!B17")
    b17["conclusion_status"] = "blocked"
    with pytest.raises(ReviewScopeError, match="B17 must remain unresolved"):
        validate_southwest_conclusions(mutated["rows"])

    # B15/B17 must stay unresolved (accounting abstention), never blocked.
    mutated = deepcopy(sw)
    mutated["rows"] = [r for r in mutated["rows"] if r["reference"] != "Input sheet!B15"]
    with pytest.raises(ReviewScopeError, match="B15 must remain unresolved"):
        validate_southwest_conclusions(mutated["rows"])

    # An unknown status is rejected.
    mutated = deepcopy(sw)
    mutated["rows"][0]["conclusion_status"] = "approved"
    with pytest.raises(ReviewScopeError, match="unknown conclusion_status"):
        validate_southwest_conclusions(mutated["rows"])

    # The cross-boundary validator rejects the false shape: a lease-converter
    # route claiming blocked-by-deferral.
    bad_routes = deepcopy(scope_json["cross_boundary_routes"])
    lease = next(r for r in bad_routes if r["id"] == "LEASE_CONVERTER_FEEDS_IN_SCOPE_COST_B45")
    lease["blocked_by_deferral"] = True
    with pytest.raises(ReviewScopeError, match="must not be blocked by deferral"):
        validate_cross_boundary_routes(bad_routes)

    # The coverage gap must name the exact 19 deferred refs, not an invented subset.
    bad_routes = deepcopy(scope_json["cross_boundary_routes"])
    coverage = next(r for r in bad_routes if r["id"] == "TERMINAL_REVIEW_COVERAGE_DEFERRED_INPUT_B45_PLUS")
    coverage["deferred_refs"] = ["Input sheet!B45"]
    with pytest.raises(ReviewScopeError, match="exact 19 deferred refs"):
        validate_cross_boundary_routes(bad_routes)
