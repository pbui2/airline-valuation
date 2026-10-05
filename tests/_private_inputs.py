"""Shared private-input gating for the public portfolio export.

The public export deliberately omits the owner's private inputs: the source
workbook (``fcffsimpleginzu.xlsx``), the private example manifest/scenario
JSON, the private policy-draft JSON/MD packet (v1 and v2), the private
draft-scenario fixture, the committed review-artifact generations, and the
private review/status docs.  Tests that genuinely depend on those inputs are
gated per-test (never as a whole module) with the markers below so a clean
clone skips them explicitly instead of failing.  Unit tests in the same files
that do not need private inputs still run; nothing here is a blanket
module-level skip.

Each marker's conditions name the EXACT files (or file families) a test needs,
never a catch-all like "any policy JSON": a partially restored clone (e.g. the
workbook plus only the v1 policy JSON but no v2 packet or review artifacts)
must still gate the affected tests with a reason that identifies the actually
missing input.

The per-test mapping lives in :data:`GATE_BY_FILE` (module basename -> marker
-> test function names).  ``tests/conftest.py`` applies these gates at
collection time.  When ``AIRLINE_VALUATION_REQUIRE_PRIVATE_INPUTS=1`` the same
gates turn into hard failures instead of skips, so strict private-integration
runs fail clearly when a required input is absent rather than silently
skipping coverage.

Gates resolve file paths through the :data:`PRIVATE_PATHS` registry at
evaluation time (not at import time), so tests can safely simulate partial
restoration by monkeypatching registry entries with temporary synthetic files
— they never need to copy the original workbook.
"""

from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

PRIVATE_WORKBOOK = ROOT / "fcffsimpleginzu.xlsx"
PRIVATE_MANIFEST = ROOT / "examples" / "fy2025-manifest.json"
PRIVATE_SCENARIO = ROOT / "examples" / "fy2025-provisional-scenario.json"
PRIVATE_POLICY_DIR = ROOT / "docs" / "policy-drafts"
PRIVATE_DRAFT_JSON = PRIVATE_POLICY_DIR / "ginzu-input-policy-v1.json"  # v1 input-policy JSON
PRIVATE_DRAFT_MD = ROOT / "docs" / "ginzu-input-policy-v1.md"           # v1 input-policy MD
PRIVATE_FIXTURE = ROOT / "tests" / "fixtures" / "ginzu_draft_scenarios_v1.json"

# v2 scope / new-cell inventory / input-policy packet (private).
PRIVATE_SCOPE_JSON = PRIVATE_POLICY_DIR / "ginzu-review-scope-v2.json"
PRIVATE_INVENTORY_JSON = PRIVATE_POLICY_DIR / "ginzu-new-cell-inventory-v2.json"
PRIVATE_INPUT_POLICY_JSON = PRIVATE_POLICY_DIR / "ginzu-input-policy-v2.json"
PRIVATE_INPUT_POLICY_MD = ROOT / "docs" / "ginzu-input-policy-v2.md"
PRIVATE_SOUTHWEST_JSON = PRIVATE_POLICY_DIR / "ginzu-southwest-review-v2.json"
PRIVATE_REVIEW_MANIFEST_V1_JSON = PRIVATE_POLICY_DIR / "ginzu-review-manifest-v1.json"
PRIVATE_POLICY_REVIEW_PACKET_V1_MD = ROOT / "docs" / "ginzu-policy-review-packet-v1.md"

# Committed review-artifact generations (xlsx + manifest) that are excluded
# from the public export.
PRIVATE_V1_REVIEW_ARTIFACT = ROOT / "review-artifacts" / "ginzu-policy-proposed-review-only.xlsx"
PRIVATE_V1_REVIEW_MANIFEST = ROOT / "review-artifacts" / "ginzu-policy-proposed-review-only-manifest.json"
PRIVATE_V2_REVIEW_ARTIFACT = ROOT / "review-artifacts" / "ginzu-policy-proposed-review-only-v2.xlsx"
PRIVATE_V2_REVIEW_MANIFEST = ROOT / "review-artifacts" / "ginzu-policy-proposed-review-only-v2-manifest.json"

# Private review/policy docs that are intentionally not shipped publicly.
PRIVATE_REVIEW_DOCS = [
    ROOT / "docs" / "provisional-comment-review.md",
    ROOT / "docs" / "provisional-input-scenario.md",
    ROOT / "docs" / "evidence-reconciliation-2025.md",
    ROOT / "docs" / "ginzu-input-policy-v1.md",
    ROOT / "docs" / "ginzu-input-policy-v2.md",
    ROOT / "docs" / "southwest-input-review.md",
    ROOT / "docs" / "southwest-valuation-decision-packet.md",
    ROOT / "docs" / "workbook-map.md",
]

# Current status docs whose v2 display-repair passage is asserted by
# test_review_copy_v2.py::test_v2_committed_artifact_matches_generated_and_docs_current.
# They are private-bound (not shipped in docs/ of the public export).
PRIVATE_CURRENT_DOCS = [
    ROOT / "docs" / "RUNBOOK.md",
    ROOT / "docs" / "PROGRESS.md",
    ROOT / "docs" / "BACKLOG.md",
    ROOT / "docs" / "CHANGES.md",
    ROOT / "docs" / "MAINTENANCE_CHECKLIST.md",
]

REQUIRE_PRIVATE_INPUTS_ENV = "AIRLINE_VALUATION_REQUIRE_PRIVATE_INPUTS"


# Symbolic registry: gates resolve these entries at evaluation time, so the
# partial-restoration regression can monkeypatch entries with temporary
# synthetic files (gating only checks presence) instead of copying the
# original workbook.
PRIVATE_PATHS: dict[str, Path] = {
    "workbook": PRIVATE_WORKBOOK,
    "manifest": PRIVATE_MANIFEST,
    "scenario": PRIVATE_SCENARIO,
    "draft_json_v1": PRIVATE_DRAFT_JSON,
    "draft_md_v1": PRIVATE_DRAFT_MD,
    "fixture": PRIVATE_FIXTURE,
    "scope_json_v2": PRIVATE_SCOPE_JSON,
    "inventory_json_v2": PRIVATE_INVENTORY_JSON,
    "input_policy_json_v2": PRIVATE_INPUT_POLICY_JSON,
    "input_policy_md_v2": PRIVATE_INPUT_POLICY_MD,
    "southwest_json_v2": PRIVATE_SOUTHWEST_JSON,
    "review_manifest_v1_json": PRIVATE_REVIEW_MANIFEST_V1_JSON,
    "policy_review_packet_v1_md": PRIVATE_POLICY_REVIEW_PACKET_V1_MD,
    "southwest_input_review_md": ROOT / "docs" / "southwest-input-review.md",
    "v1_review_artifact": PRIVATE_V1_REVIEW_ARTIFACT,
    "v1_review_manifest": PRIVATE_V1_REVIEW_MANIFEST,
    "v2_review_artifact": PRIVATE_V2_REVIEW_ARTIFACT,
    "v2_review_manifest": PRIVATE_V2_REVIEW_MANIFEST,
    "runbook_doc": ROOT / "docs" / "RUNBOOK.md",
    "progress_doc": ROOT / "docs" / "PROGRESS.md",
    "backlog_doc": ROOT / "docs" / "BACKLOG.md",
    "changes_doc": ROOT / "docs" / "CHANGES.md",
    "maintenance_doc": ROOT / "docs" / "MAINTENANCE_CHECKLIST.md",
    # test_foundation.py review-doc family: the exact docs each foundation test
    # reads.  AGENTS.md, docs/modules/python.md and the progress/todo/DECISIONS/
    # requirements/plan status docs are private (absent from the public export);
    # PROJECT.md/ARCHITECTURE.md/source-policy.md/README.md are public but are
    # registered so the foundation gates name the exact per-test read set and a
    # single restored doc can never enable a test that needs others.
    "agents_doc": ROOT / "AGENTS.md",
    "modules_python_doc": ROOT / "docs" / "modules" / "python.md",
    "project_doc": ROOT / "docs" / "PROJECT.md",
    "architecture_doc": ROOT / "docs" / "ARCHITECTURE.md",
    "source_policy_doc": ROOT / "docs" / "source-policy.md",
    "readme_doc": ROOT / "README.md",
    "decisions_doc": ROOT / "docs" / "DECISIONS.md",
    "todo_doc": ROOT / "docs" / "todo.md",
    "progress_lower_doc": ROOT / "docs" / "progress.md",
    "requirements_doc": ROOT / "docs" / "requirements.md",
    "plan_doc": ROOT / "docs" / "plan.md",
    # test_autonomous_target_docs / test_detailed_documentation /
    # test_provisional_documentation: the exact docs each documentation test
    # reads.  run-guide.md is public; the provisional-input-scenario /
    # evidence-reconciliation-2025 / provisional-comment-review docs are
    # private review docs (also listed in PRIVATE_REVIEW_DOCS).  They are
    # registered so each documentation gate names the exact per-test read set
    # and a single restored doc can never enable a test that needs others.
    "run_guide_doc": ROOT / "docs" / "run-guide.md",
    "provisional_scenario_doc": ROOT / "docs" / "provisional-input-scenario.md",
    "evidence_reconciliation_doc": ROOT / "docs" / "evidence-reconciliation-2025.md",
    "provisional_comment_review_doc": ROOT / "docs" / "provisional-comment-review.md",
}


def _any_review_doc_present() -> bool:
    """Condition for 'needs_private_review_docs': at least one doc present."""
    return any(path.is_file() for path in PRIVATE_REVIEW_DOCS)


def _full_private_docs_present() -> bool:
    """Condition for the complete private review-doc set being restored.

    ``test_autonomous_policy_vocabulary_and_trace`` reads ARCHITECTURE.md and
    source-policy.md whose autonomous-target policy vocabulary exists only in
    the full private versions — the public export ships curated copies that
    lack it.  Presence of those two paths alone therefore cannot prove the test
    is enabled; only the full private review-doc set coexists with the full
    docs, so a single restored review doc must never enable the test.
    """
    return all(path.is_file() for path in PRIVATE_REVIEW_DOCS)


# marker name -> (human label, [conditions that must ALL hold])
# A condition is a symbolic PRIVATE_PATHS key or a zero-arg callable.  The
# label names the whole dependency group; the per-file detail (which file is
# actually missing) comes from missing_inputs()/gate_reason().
GATES: dict[str, tuple[str, tuple[object, ...]]] = {
    "needs_workbook": (
        "source workbook fcffsimpleginzu.xlsx",
        ("workbook",),
    ),
    "needs_manifest": (
        "example manifest examples/fy2025-manifest.json",
        ("manifest",),
    ),
    "needs_scenario": (
        "example scenario examples/fy2025-provisional-scenario.json",
        ("scenario",),
    ),
    "needs_workbook_manifest": (
        "source workbook and example manifest",
        ("workbook", "manifest"),
    ),
    "needs_workbook_scenario": (
        "source workbook and example scenario",
        ("workbook", "scenario"),
    ),
    "needs_policy_drafts_fixture": (
        "policy-draft JSON and draft scenario fixture",
        ("draft_json_v1", "fixture"),
    ),
    # v1 review copy: generation/verification hashes the pinned workbook AND
    # both the exact v1 policy JSON and MD (docs/policy-drafts/ginzu-input-policy-v1.json
    # and docs/ginzu-input-policy-v1.md) against pinned constants.
    "needs_workbook_v1_policy": (
        "source workbook, v1 policy JSON and v1 policy MD",
        ("workbook", "draft_json_v1", "draft_md_v1"),
    ),
    # v1-note-only tests read the exact v1 policy JSON but never the workbook.
    "needs_v1_policy_json": (
        "v1 policy JSON docs/policy-drafts/ginzu-input-policy-v1.json",
        ("draft_json_v1",),
    ),
    # v2 review copy: generation/verification hashes the pinned workbook AND
    # the exact v2 scope/input-policy packet (ginzu-review-scope-v2.json,
    # ginzu-input-policy-v2.json, ginzu-input-policy-v2.md).
    "needs_workbook_v2_packet": (
        "source workbook and the exact v2 scope/input-policy packet",
        ("workbook", "scope_json_v2", "input_policy_json_v2", "input_policy_md_v2"),
    ),
    # v2 exact-delta test additionally compares the generated note against the
    # exact v1 policy JSON.
    "needs_workbook_v2_packet_v1": (
        "source workbook, exact v2 scope/input-policy packet and v1 policy JSON",
        ("workbook", "scope_json_v2", "input_policy_json_v2",
         "input_policy_md_v2", "draft_json_v1"),
    ),
    # Committed v2 artifact/manifest verification plus the current-docs display
    # passage needs the v2 packet, the committed v2 artifact/manifest and all
    # five current status docs.
    "needs_workbook_v2_artifacts": (
        "source workbook, exact v2 packet, committed v2 artifact/manifest and current docs",
        ("workbook", "scope_json_v2", "input_policy_json_v2", "input_policy_md_v2",
         "v2_review_artifact", "v2_review_manifest",
         "runbook_doc", "progress_doc", "backlog_doc", "changes_doc", "maintenance_doc"),
    ),
    # v2 mutation/alias tests also pin the committed v1 artifact/manifest.
    "needs_workbook_v2_packet_v1_artifacts": (
        "source workbook, exact v2 packet and committed v1 artifact/manifest",
        ("workbook", "scope_json_v2", "input_policy_json_v2", "input_policy_md_v2",
         "v1_review_artifact", "v1_review_manifest"),
    ),
    # v2 baseline-hash pinning reads the committed v1 artifact/manifest directly.
    "needs_workbook_v1_artifacts": (
        "source workbook and committed v1 artifact/manifest",
        ("workbook", "v1_review_artifact", "v1_review_manifest"),
    ),
    # review_scope_v2 AC.1 scope/set checks: v2 scope JSON + v1/v2 policy JSON.
    "needs_scope_policy_v2_v1": (
        "v2 scope JSON, v2 input-policy JSON and v1 input-policy JSON",
        ("scope_json_v2", "input_policy_json_v2", "draft_json_v1"),
    ),
    # review_scope_v2 AC.2 validator: v2 + v1 input-policy JSON.
    "needs_v2_policy_v1": (
        "v2 and v1 input-policy JSON",
        ("input_policy_json_v2", "draft_json_v1"),
    ),
    # review_scope_v2 AC.2 inventory: workbook + the checked-in inventory JSON.
    "needs_workbook_inventory": (
        "source workbook and v2 new-cell-inventory JSON",
        ("workbook", "inventory_json_v2"),
    ),
    # review_scope_v2 AC.1 approved-baseline pinning reads the whole v1 baseline
    # packet plus the v2 input-policy JSON.
    "needs_v2_baseline_packet": (
        "v2 input-policy JSON and the v1 baseline packet (v1 policy JSON/MD, "
        "review manifest v1, policy packet v1, southwest review MD, v1 review manifest)",
        ("input_policy_json_v2", "draft_json_v1", "draft_md_v1",
         "review_manifest_v1_json", "policy_review_packet_v1_md",
         "southwest_input_review_md", "v1_review_manifest"),
    ),
    # review_scope_v2 AC.3: v2 scope JSON + southwest-review JSON.
    "needs_scope_southwest": (
        "v2 scope JSON and v2 southwest-review JSON",
        ("scope_json_v2", "southwest_json_v2"),
    ),
    "needs_private_review_docs": (
        "review docs (e.g. docs/provisional-comment-review.md)",
        (_any_review_doc_present,),
    ),
    # test_foundation.py review-doc family: exact per-test doc dependencies.
    # Each foundation test gates on the exact docs it reads (never an any-of
    # family), so restoring a single unrelated review doc can never enable a
    # test that needs other docs.  A clean clone skips all three; strict mode
    # fails them clearly on the actually missing doc.
    "needs_foundation_metadata_docs": (
        "foundation metadata docs (AGENTS.md, docs/PROJECT.md, docs/modules/python.md)",
        ("agents_doc", "project_doc", "modules_python_doc"),
    ),
    "needs_foundation_required_docs": (
        "all foundation-required docs (README, AGENTS, PROJECT, ARCHITECTURE, "
        "RUNBOOK, MAINTENANCE_CHECKLIST, DECISIONS, CHANGES, BACKLOG, PROGRESS, "
        "modules/python, progress, todo, requirements, plan, source-policy)",
        ("readme_doc", "agents_doc", "project_doc", "architecture_doc",
         "runbook_doc", "maintenance_doc", "decisions_doc", "changes_doc",
         "backlog_doc", "progress_doc", "modules_python_doc",
         "progress_lower_doc", "todo_doc", "requirements_doc", "plan_doc",
         "source_policy_doc"),
    ),
    "needs_foundation_progress_docs": (
        "foundation progress/todo docs (docs/PROGRESS.md, docs/progress.md, "
        "docs/todo.md, docs/BACKLOG.md)",
        ("progress_doc", "progress_lower_doc", "todo_doc", "backlog_doc"),
    ),
    # test_autonomous_target_docs.py / test_detailed_documentation.py /
    # test_provisional_documentation.py: exact per-test docs/workbook deps.
    # Each test gates on the exact docs it reads (never an any-of family), so
    # restoring a single unrelated review doc can never enable a test that
    # needs other docs.  The workbook-pin test uses needs_workbook, not a doc
    # family.  test_autonomous_policy_vocabulary_and_trace additionally
    # requires the complete private review-doc set because the vocabulary it
    # asserts lives only in the full private ARCHITECTURE.md/source-policy.md
    # (the public export ships curated copies that lack it).
    "needs_autonomous_requirements_docs": (
        "autonomous requirement/report contract docs "
        "(docs/PROJECT.md, docs/requirements.md)",
        ("project_doc", "requirements_doc"),
    ),
    "needs_autonomous_policy_docs": (
        "autonomous-target policy vocabulary in the full private "
        "ARCHITECTURE.md and source-policy.md (all private review docs restored)",
        ("architecture_doc", "source_policy_doc", _full_private_docs_present),
    ),
    "needs_autonomous_trial_gates_docs": (
        "autonomous trial-gate docs (PROJECT, PROGRESS, RUNBOOK, run-guide, "
        "MAINTENANCE_CHECKLIST, requirements, CHANGES)",
        ("project_doc", "progress_doc", "runbook_doc", "run_guide_doc",
         "maintenance_doc", "requirements_doc", "changes_doc"),
    ),
    "needs_decision_history_docs": (
        "decision history doc (docs/DECISIONS.md)",
        ("decisions_doc",),
    ),
    "needs_detailed_workflow_docs": (
        "detailed workflow docs (PROJECT, ARCHITECTURE, provisional-input-scenario, "
        "run-guide, source-policy, DECISIONS, PROGRESS, BACKLOG, RUNBOOK, "
        "MAINTENANCE_CHECKLIST)",
        ("project_doc", "architecture_doc", "provisional_scenario_doc",
         "run_guide_doc", "source_policy_doc", "decisions_doc", "progress_doc",
         "backlog_doc", "runbook_doc", "maintenance_doc"),
    ),
    "needs_detailed_ac6_docs": (
        "detailed AC.6 status docs "
        "(docs/DECISIONS.md, docs/run-guide.md, docs/PROJECT.md)",
        ("decisions_doc", "run_guide_doc", "project_doc"),
    ),
    "needs_provisional_review_docs": (
        "provisional review docs (DECISIONS, BACKLOG, provisional-input-scenario, "
        "evidence-reconciliation-2025, provisional-comment-review, CHANGES, "
        "PROGRESS, RUNBOOK, run-guide)",
        ("decisions_doc", "backlog_doc", "provisional_scenario_doc",
         "evidence_reconciliation_doc", "provisional_comment_review_doc",
         "changes_doc", "progress_doc", "runbook_doc", "run_guide_doc"),
    ),
}


def _condition_missing(condition: object) -> str | None:
    """Return a missing-input description for one condition, else None."""
    if callable(condition):
        return None if condition() else "<none present>"
    path = PRIVATE_PATHS[condition]
    if path.is_file():
        return None
    return f"{path.name} at {path}"


def missing_inputs(marker: str) -> list[str]:
    """Return the human-readable missing-input descriptions for a marker."""
    label, conditions = GATES[marker]
    missing: list[str] = []
    for condition in conditions:
        detail = _condition_missing(condition)
        if detail == "<none present>":
            # A callable family condition failed: the marker label already
            # names the family precisely.
            missing.append(label)
        elif detail is not None:
            missing.append(detail)
    return missing


def gate_reason(marker: str) -> str | None:
    """Return the skip/fail reason when private inputs are absent, else None."""
    label, conditions = GATES[marker]
    missing = missing_inputs(marker)
    if not missing:
        return None
    detail = ", ".join(missing)
    return (
        f"requires private {label} "
        f"(not distributed in the public export): {detail}"
    )


def _mark(path: Path, label: str):
    return pytest.mark.skipif(
        not path.is_file(),
        reason=(
            f"requires private {label} "
            f"(not distributed in the public export): {path}"
        ),
    )


def _mark_any(paths: list[Path], label: str):
    return pytest.mark.skipif(
        not any(p.is_file() for p in paths),
        reason=(
            f"requires private {label} "
            f"(not distributed in the public export): one of {paths}"
        ),
    )


# Backwards-compatible marker aliases used by earlier versions of the gating
# helper.  New code should rely on GATE_BY_FILE + gate_reason().
needs_workbook = _mark(PRIVATE_WORKBOOK, "source workbook fcffsimpleginzu.xlsx")
needs_manifest = _mark(PRIVATE_MANIFEST, "example manifest examples/fy2025-manifest.json")
needs_scenario = _mark(PRIVATE_SCENARIO, "example scenario examples/fy2025-provisional-scenario.json")
needs_policy_drafts = _mark(PRIVATE_DRAFT_JSON, "policy-draft JSON docs/policy-drafts/ginzu-input-policy-v1.json")
needs_private_fixture = _mark(PRIVATE_FIXTURE, "draft scenario fixture tests/fixtures/ginzu_draft_scenarios_v1.json")
needs_private_review_docs = _mark_any(
    PRIVATE_REVIEW_DOCS, "private review docs (e.g. docs/provisional-comment-review.md)"
)


# ---------------------------------------------------------------------------
# Per-test gate map: module basename -> marker -> exact test function names.
# A module may use several markers so each test gates on exactly the private
# inputs it reads (never a whole-module blanket skip).
# ---------------------------------------------------------------------------
GATE_BY_FILE = {
    'test_acceptance.py': {
        "needs_workbook_manifest": {
            'test_acceptance_contract_lookup_mode_is_blocked',
            'test_acceptance_finite_template_cache_without_signoff_stays_unverified',
            'test_acceptance_invalid_share_count_is_rejected',
            'test_acceptance_manifest_validation_never_uses_network',
            'test_acceptance_missing_financial_fact_blocks_readiness',
            'test_acceptance_no_lookahead_rejects_post_cutoff_publication',
            'test_acceptance_original_filing_and_amendment_scope',
            'test_acceptance_pinned_cutoff_and_source_times',
            'test_acceptance_protected_formula_mutation_is_rejected',
            'test_acceptance_review_pending_and_prepare_blocked',
            'test_inspect_synthetic_negative_feature_or_formula_mutation_is_unverified',
            'test_inspect_synthetic_negative_missing_error_nonfinite_cached_output_is_unverified',
            'test_inspect_synthetic_negative_saved_input_mutation_is_unverified',
            'test_prepare_converts_whole_share_source_to_millions',
            'test_prepare_populates_approved_inputs',
        },
    },
    'test_autonomous_target_docs.py': {
        # Each documentation test gates on the exact docs it reads; the
        # workbook-pin test gates on the workbook (needs_workbook), never on a
        # doc family.  test_autonomous_policy_vocabulary_and_trace reads
        # ARCHITECTURE.md/source-policy.md whose autonomous-target vocabulary
        # lives only in the full private versions, so it additionally requires
        # the complete private review-doc set.
        "needs_autonomous_requirements_docs": {
            'test_autonomous_requirement_and_report_contract',
        },
        "needs_autonomous_policy_docs": {
            'test_autonomous_policy_vocabulary_and_trace',
        },
        "needs_autonomous_trial_gates_docs": {
            'test_autonomous_target_does_not_claim_implementation_or_erase_trial_gates',
        },
        "needs_decision_history_docs": {
            'test_decision_history_preserved',
        },
        "needs_workbook": {
            'test_documentation_amendment_preserves_pinned_workbook',
        },
    },
    'test_cli.py': {
        "needs_workbook": {
            'test_attestation_missing_is_recorded_in_report',
            'test_inspect_malformed_saved_zip_writes_unverified_report',
            'test_inspect_pending_manifest_fails_closed_before_workbook_checks',
            'test_inspect_real_template_missing_attestation_is_unverified',
            'test_inspect_rejects_prepared_path_outside_run_directory',
            'test_pending_unmapped_manifest_review_exits_zero',
            'test_prepare_backup_cleanup_failure_keeps_published_generation_successful',
            'test_prepare_overwrite_publish_failure_restores_previous_run',
            'test_prepare_populates_normalized_ledger_checklist_and_run_state',
            'test_prepare_stage_validation_failure_preserves_old_run',
            'test_review_overwrite_is_explicit',
        },
    },
    'test_detailed_candidate.py': {
        "needs_workbook": {
            'test_AC3_synthetic_candidate_has_exact_contract_delta_and_independent_evidence',
            'test_synthetic_candidate_rolls_back_on_publish_failure',
        },
    },
    'test_detailed_documentation.py': {
        "needs_detailed_workflow_docs": {
            'test_detailed_workflow_documentation_and_status',
        },
        "needs_detailed_ac6_docs": {
            'test_ac6_superseding_detailed_status_is_documented',
        },
    },
    'test_detailed_end_to_end.py': {
        "needs_workbook": {
            'test_malformed_saved_workbook_reports_structural_repair_diagnostic',
            'test_real_cached_error_fault_is_reported_without_recalculation',
            'test_real_formula_fault_reports_pinned_formula_diagnostic',
            'test_real_lookup_fault_reports_pinned_lookup_diagnostic',
            'test_real_run_state_hash_fault_reports_binding_diagnostic',
            'test_self_consistent_relabelled_synthetic_package_fails_provenance',
            'test_synthetic_candidate_reaches_real_auditor_and_saved_copy_stays_uncalculated',
        },
    },
    'test_detailed_inspect.py': {
        "needs_workbook": {
            'test_detailed_inspect_accepts_external_saved_copy_without_promoting_status',
            'test_detailed_inspect_hash_consistent_pinned_candidate_negative_control_is_blocked',
            'test_detailed_inspect_rejects_changed_allowlisted_numeric_input_in_saved_copy',
            'test_detailed_inspect_rejects_extra_legacy_artifacts',
            'test_detailed_inspect_rejects_saved_copy_symlink_and_traversal',
            'test_detailed_inspect_rejects_symlink_candidate',
            'test_detailed_inspect_reports_stale_optional_saved_copy_hash',
        },
    },
    'test_detailed_inventory.py': {
        "needs_workbook": {
            'test_formula_cells_cannot_be_offered_as_input_driver',
            'test_pinned_selected_route_audits_positive_without_certifying_stale_caches',
            'test_route_evidence_fails_closed_on_tampering',
        },
    },
    'test_draft_policy.py': {
        "needs_policy_drafts_fixture": {
            'test_all_evidence_link_inspector_digests_match_documented_canonicalization',
            'test_draft_b25_policy_never_asserts_operating_countries_route',
            'test_draft_branch_and_formula_metadata',
            'test_draft_conflict_and_precedence_trace_semantics',
            'test_draft_evaluator_fails_closed_on_evidence_rights_and_approval_gaps',
            'test_draft_fallbacks_are_typed_and_review_only',
            'test_draft_formula_guarded_cells_have_typed_formula_gates',
            'test_draft_inventory_matches_detailed_contract',
            'test_draft_is_not_operational',
            'test_draft_policy_mutation_sensitivity',
            'test_draft_policy_scenarios',
            'test_draft_policy_schema_and_trace',
            'test_draft_route_gate_evaluates_contract_allowed_b25_domain',
            'test_draft_route_gate_never_grants_rights_when_unresolved_or_inactive',
            'test_draft_route_gate_references_resolve_to_contract_selector_domains',
            'test_draft_same_tier_agreeing_duplicates_are_rejected_not_selected',
            'test_draft_selected_sources_distinguished_from_rejected',
            'test_draft_selector_entry_evaluation_is_not_numeric',
            'test_draft_workbook_pin_unchanged',
            'test_draft_wrong_type_demo_cannot_bypass_selector_entry_gate',
            'test_draft_wrong_type_demo_cannot_bypass_selector_gate_on_direct_low_level_evaluate',
            'test_entry_evaluation_is_reference_bound_and_unresolved_entries_stay_unresolved',
            'test_policy_review_packet_coverage',
        },
    },
    'test_evidence_review.py': {
        "needs_workbook": {
            'test_generator_does_not_change_model_template',
        },
    },
    'test_foundation.py': {
        # Each foundation test gates on the exact docs it reads, so a single
        # restored review doc cannot enable tests that need other docs.
        "needs_foundation_metadata_docs": {
            'test_foundation_metadata',
        },
        "needs_foundation_required_docs": {
            'test_required_docs_and_links',
        },
        "needs_foundation_progress_docs": {
            'test_progress_todo_schema',
        },
    },
    'test_manifest.py': {
        "needs_manifest": {
            'test_approved_manifest_cannot_bypass_required_contract_cells_with_empty_list',
            'test_b21_fixed_february_3_observation_remains_source_observation',
            'test_both_publication_and_availability_times_are_checked',
            'test_delayed_publication_after_cutoff_is_rejected_and_unknown_blocks_ready',
            'test_duplicate_mapping_rejected',
            'test_example_is_reviewable_but_not_ready',
            'test_extra_protected_or_formula_cell_mapping_rejected',
            'test_financial_facts_must_use_original_not_amendment_authority',
            'test_numeric_currency_requires_explicit_usd_millions_unit',
            'test_pending_filing_blocks_readiness_even_if_inputs_approved',
            'test_source_url_must_match_accession',
            'test_supported_selector_and_branch_input_are_contract_required',
            'test_treasury_observation_time_unknown_stays_pending',
            'test_wrong_input_unit_and_rate_range_rejected',
            'test_zero_numeric_value_requires_explicit_semantics',
        },
    },
    'test_provisional_cli.py': {
        "needs_workbook": {
            'test_inspect_rejects_guarded_d62_formula_after_rename_without_mutation',
            'test_prepare_rejects_provisional_scenario_json_without_output',
            'test_validate_rejects_provisional_scenario_json',
        },
    },
    'test_provisional_documentation.py': {
        "needs_provisional_review_docs": {
            'test_reviewable_decision_record_and_automation_backlog',
        },
    },
    'test_provisional_formula_conversion.py': {
        "needs_workbook_scenario": {
            'test_changed_dependent_formula_is_rejected_by_pin',
            'test_formula_conversion_contract_source_xml_must_match_exact_pin',
            'test_formula_to_numeric_cells_require_separate_declarations_and_are_exact',
            'test_mixed_column_dependent_range_is_detected',
            'test_shared_or_array_range_intersecting_targets_fails_closed',
        },
    },
    'test_provisional_publication.py': {
        "needs_workbook_scenario": {
            'test_amended_provisional_cli_publication',
            'test_amended_provisional_exact_delta_and_isolation',
            'test_amended_provisional_failure_preserves_previous_artifacts',
            'test_amended_provisional_notice_and_ledger_status',
            'test_provisional_detailed_populates_verified_drivers',
            'test_provisional_missing_candidate_inventory_no_publication',
            'test_provisional_overwrite_recovery',
            'test_provisional_rejects_invalid_detailed_driver',
            'test_workflows_refuse_cross_class_overwrite',
        },
    },
    'test_provisional_scenario.py': {
        "needs_scenario": {
            'test_c26_c32_label_to_input_mapping',
            'test_candidate_dispositions_are_returned_and_required',
            'test_candidate_inventory_is_required',
            'test_example_loads_normalized_detailed_mapping',
            'test_example_uses_exact_whitelisted_sheet_names_even_when_regex_accepts_case',
            'test_maturity_domain_and_other_positive_drivers',
            'test_north_america_only_allocation_and_provenance',
            'test_operator_lease_cash_and_nonoperating_decisions',
            'test_preferred_inputs_require_explicit_per_share_units',
            'test_preferred_one_accepts_nonzero_price_and_nonnegative_dividend',
            'test_preferred_zero_requires_blank_values_and_one_requires_valid_values',
            'test_rejects_cost_of_capital_input_and_wrong_units',
            'test_rejects_formula_backed_input_cell',
            'test_rejects_mismatched_industry_and_rating_lookup_labels',
            'test_rejects_overlapping_aggregate_region_even_when_sum_matches',
            'test_rejects_published_debt_cash_and_nonoperating_mutations',
            'test_rejects_region_allocation_not_equal_to_revenue',
            'test_rejects_regional_split_preserving_revenue_total',
            'test_rejects_stale_provenance_for_constrained_values',
            'test_rejects_unknown_refs_and_missing_required',
            'test_seven_growth_profitability_assumptions_and_provenance',
            'test_zero_maturity_rating_and_selected_route',
        },
    },
    'test_provisional_structural_guards.py': {
        "needs_scenario": {
            'test_pristine_cost_route_audit_failure_prevents_publication',
            'test_pristine_input_structure_failure_prevents_publication',
            'test_staged_input_structure_failure_prevents_publication_and_cleans_stage',
        },
    },
    'test_provisional_writer.py': {
        "needs_workbook": {
            'test_contract_covers_each_direct_scenario_cell_and_pins_dependencies',
            'test_preferred_guard_rejects_inconsistent_inputs',
            'test_provisional_write_exact_allowed_delta_and_cached_formula',
            'test_verify_rejects_unrequested_member_and_cell_mutations',
            'test_verify_rejects_zip_order_and_metadata_mutations',
            'test_verify_rejects_zip_prefix_suffix_and_header_mutations',
            'test_writer_rejects_formula_cell_on_altered_copy_even_if_contract_selected',
            'test_writer_rejects_unsupported_or_unrepresented_cells',
        },
    },
    'test_review_copy.py': {
        # Alias-protection tests only read the pinned workbook: the output
        # guard fires before any draft is loaded.
        "needs_workbook": {
            'test_review_copy_protects_operational_source_path',
            'test_review_copy_rejects_output_aliasing_source',
            'test_review_copy_rejects_symlink_and_hardlink_aliases',
            'test_review_copy_rejects_tmp_sibling_alias',
        },
        # The v1-note fidelity test builds notes from the v1 policy JSON only.
        "needs_v1_policy_json": {
            'test_review_copy_note_full_fidelity_question_and_excel_safety',
        },
        # Everything else in review_copy needs the workbook + exact v1 policy
        # JSON and MD (generation hashes both against pinned constants).
        "needs_workbook_v1_policy": {
            'test_review_copy_cli_generate_and_verify',
            'test_review_copy_delta_and_thread_integrity',
            'test_review_copy_draft_check_immune_to_et_namespace_registration',
            'test_review_copy_draft_guard_uses_resolved_root_and_preserves_drafts',
            'test_review_copy_et_namespace_registry_restored_after_inspection',
            'test_review_copy_manifest_and_status',
            'test_review_copy_manifest_full_equality_rejects_tampering',
            'test_review_copy_mutation_sensitivity',
            'test_review_copy_nonoperational_boundary',
            'test_review_copy_note_content',
            'test_review_copy_pins_and_note_coverage',
            'test_review_copy_preserves_original_comments',
            'test_review_copy_rejects_draft_and_contract_aliasing',
            'test_review_copy_rejects_target_thread_contract_tampering',
            'test_review_copy_rejects_unsafe_inputs',
        },
    },
    'test_review_copy_v2.py': {
        "needs_workbook_v2_packet": {
            'test_v2_manifest_and_document_status',
            'test_v2_review_copy_fixed_root_bindings_and_anchors',
            'test_v2_review_copy_preserves_originals',
            'test_v2_review_copy_semantic_verify_without_manifest',
        },
        # The exact-delta test also compares the generated note against the
        # exact v1 input-policy JSON.
        "needs_workbook_v2_packet_v1": {
            'test_v2_review_copy_exact_delta_and_notes',
        },
        "needs_workbook_v2_artifacts": {
            'test_v2_committed_artifact_matches_generated_and_docs_current',
        },
        "needs_workbook_v2_packet_v1_artifacts": {
            'test_v2_review_copy_rejects_mutations_and_aliases',
        },
        "needs_workbook_v1_artifacts": {
            'test_v2_review_copy_pins_baseline_hashes',
        },
    },
    'test_review_scope_v2.py': {
        "needs_scope_policy_v2_v1": {
            'test_ac1_scope_ordered_unique_58_with_independent_sets_and_exact_deferred',
        },
        "needs_v2_baseline_packet": {
            'test_ac1_approved_baselines_pinned_fixed_refs_and_mutation_rejection',
        },
        "needs_workbook_inventory": {
            'test_ac2_new_cell_inventory_exact_ten_and_negative_cases',
        },
        "needs_v2_policy_v1": {
            'test_ac2_validator_rejects_swapped_period_zero_from_absence_and_mismatched_ids',
        },
        "needs_scope_southwest": {
            'test_ac3_southwest_58_rows_honest_statuses_and_established_locators_only',
            'test_ac3_no_false_blocked_by_deferral_and_coverage_gap_validation',
        },
    },
    'test_trial_guidance.py': {
        "needs_workbook": {
            'test_conversion_and_non_input_guards_are_explicit',
            'test_cross_issuer_selector_domains',
            'test_exact_comment_and_absence_are_distinguished',
            'test_extension_selector_validations_retain_targets_and_list_sources',
            'test_guidance_comment_header_hash_mutation',
            'test_guidance_formula_and_route_classification',
            'test_guidance_inventory_all_detailed_refs',
            'test_inventory_covers_all_pinned_detailed_refs_with_hashed_evidence',
            'test_mutated_workbook_never_yields_authentic_selector_domains',
            'test_reader_fails_closed_on_a_non_pinned_workbook',
        },
    },
    'test_workbook.py': {
        "needs_workbook": {
            'test_cached_output_rejects_boolean_nonfinite_and_error_values',
            'test_conditional_inputs_activate_only_when_override_yes',
            'test_excel_calcid_and_calcchain_variation_is_permitted_but_iteration_is_not',
            'test_feature_content_mutation_and_removal_rejected',
            'test_formula_and_static_mutations_rejected',
            'test_metadata_fingerprint_is_stable_after_openpyxl_import',
            'test_names_sheet_state_and_validation_protection_are_guarded',
            'test_reader_rejects_bad_relationship_target_and_duplicate_cells',
            'test_reader_rejects_duplicate_zip_names_and_traversal',
            'test_reader_rejects_root_relationship_and_content_type_tampering',
            'test_real_contract_and_explicit_input_allowlist',
            'test_saved_mode_allows_only_inventoried_formula_followers',
            'test_saved_mode_rejects_follower_structure_mutation',
            'test_unsupported_and_unknown_switches_fail_closed',
        },
    },
    'test_writer.py': {
        "needs_workbook": {
            'test_reject_unpinned_template_and_invalid_values',
            'test_verify_rejects_changed_unrequested_cell_and_other_part',
            'test_write_prepared_exact_values_and_preservation',
            'test_writer_rejects_formula_cell_even_if_allowlisted',
            'test_writer_rejects_root_relationship_and_content_type_tampering',
        },
    },
}
