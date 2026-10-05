"""Portable public-export contract checks (AC.3 README, AC.5 strict mode).

These tests run on a clean clone and verify the public presentation contract
and the private-input strict-mode behavior:

* ``test_public_readme_contract`` (AC.3): the README credits Damodaran, links
  to the original, distinguishes download/modify from unconfirmed
  redistribution, says the fully functional version remains with the owner,
  disclaims a complete valuation, documents the optional direct download and
  pinned-SHA-256 check, and makes no misleading full-functionality claim.
* ``test_required_private_inputs_fail`` (AC.5): with
  ``AIRLINE_VALUATION_REQUIRE_PRIVATE_INPUTS=1`` a private-input test fails
  clearly instead of silently skipping.
* ``test_gate_map_is_precise_and_consistent``: every gate marker and test name
  in the per-test gate map resolves, so gating is precise (no whole-module
  blanket skips and no dangling references).
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PYTHON = Path(sys.executable)

DAMODARAN_PAGE = "https://pages.stern.nyu.edu/~adamodar/New_Home_Page/spreadsh.htm"
DAMODARAN_WORKBOOK = "https://pages.stern.nyu.edu/~adamodar/pc/fcffsimpleginzu.xlsx"


# ---------------------------------------------------------------------------
# AC.3: public README contract.
# ---------------------------------------------------------------------------


def test_public_readme_contract():
    readme = ROOT / "README.md"
    assert readme.is_file(), "README.md must exist in the public export"
    text = readme.read_text(encoding="utf-8")

    # Credit and links to the original source.
    assert "Damodaran" in text
    assert DAMODARAN_PAGE in text
    assert DAMODARAN_WORKBOOK in text

    # Distinguishes download/modification (permitted) from redistribution
    # (not established), and says the template is not redistributed here.
    assert "rehosting" in text or "redistribut" in text.lower()
    assert "does **not** include his template" in text

    # Fully functional version remains with the owner.
    assert "fully functional" in text and "owner" in text
    assert "outside" in text and "public" in text and "repository" in text

    # Disclaims a complete valuation.
    assert "not" in text and "completed valuation" in text.lower() or "complete valuation" in text.lower()

    # Optional direct download + pinned-SHA-256 check documented.
    assert "sha256" in text.lower() or "SHA-256" in text
    assert "contract.json" in text
    assert "will fail the pinned-hash checks" in text

    # No misleading full-functionality claim: README must not promise that the
    # private workbook workflows run from a bare clone.
    assert "cannot regenerate" in text or "cannot be regenerated" in text

    # Relative links in the README must resolve to files that exist here.
    _assert_relative_links_resolve(readme)


def _assert_relative_links_resolve(source: Path):
    import re

    link_pattern = re.compile(r"\[[^\]]+\]\(([^)]+)\)")
    for target in link_pattern.findall(source.read_text(encoding="utf-8")):
        if target.startswith(("http://", "https://", "mailto:", "#")):
            continue
        target_path = target.split("#", 1)[0]
        if not target_path:
            continue
        assert (source.parent / target_path).resolve().is_file(), (
            f"{source.relative_to(ROOT)} links to missing {target}"
        )


# ---------------------------------------------------------------------------
# AC.5: strict private-input mode fails clearly.
# ---------------------------------------------------------------------------


def _run_pytest(args, extra_env):
    env = dict(os.environ)
    env["PYTHONPATH"] = str(ROOT / "src")
    env.update(extra_env)
    # Never fall back to the owner's home fixtures.
    env.pop("AIRLINE_VALUATION_SEC_FIXTURE_DIR", None)
    env.pop("AIRLINE_VALUATION_REQUIRE_SEC_FIXTURES", None)
    result = subprocess.run(
        [str(PYTHON), "-m", "pytest", "-q", "-p", "no:cacheprovider", *args],
        cwd=str(ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
    )
    return result


# One gated test that requires the private source workbook.  In normal mode it
# must skip; in strict mode it must fail with an explicit reason.
_GATED_NODE = "tests/test_workbook.py::test_real_contract_and_explicit_input_allowlist"


def test_required_private_inputs_fail():
    if (ROOT / "fcffsimpleginzu.xlsx").is_file():
        pytest.skip("private workbook present; strict-absent check not applicable")

    normal = _run_pytest([_GATED_NODE], {})
    assert normal.returncode == 0, f"normal mode should skip, got:\n{normal.stdout}\n{normal.stderr}"
    assert "skipped" in normal.stdout.lower()

    strict = _run_pytest(
        [_GATED_NODE], {"AIRLINE_VALUATION_REQUIRE_PRIVATE_INPUTS": "1"}
    )
    assert strict.returncode != 0, "strict mode must fail, not skip"
    assert "requires private source workbook" in (strict.stdout + strict.stderr)


# ---------------------------------------------------------------------------
# Gate-map precision guards (no blanket module skips, no dangling refs).
# ---------------------------------------------------------------------------


def test_gate_map_is_precise_and_consistent():
    from _private_inputs import GATE_BY_FILE, GATES

    assert GATE_BY_FILE, "gate map must not be empty"
    for module_basename, by_marker in GATE_BY_FILE.items():
        assert isinstance(by_marker, dict) and by_marker, (
            f"{module_basename} must gate individual tests, not an empty map"
        )
        module_file = ROOT / "tests" / module_basename
        assert module_file.is_file(), f"gate references missing module {module_basename}"
        source = module_file.read_text(encoding="utf-8")
        for marker, tests in by_marker.items():
            assert marker in GATES, f"{module_basename} uses unknown marker {marker!r}"
            assert isinstance(tests, set) and tests, (
                f"{module_basename}/{marker} must gate a non-empty set of tests"
            )
            # Every gated test name must actually exist in the module source.
            for test_name in tests:
                assert f"def {test_name}(" in source, (
                    f"{module_basename} gate lists nonexistent test {test_name!r}"
                )


def test_gate_reasons_are_explicit():
    """On a clean clone every active gate yields an explicit missing-input reason."""
    from _private_inputs import GATE_BY_FILE, gate_reason

    markers = {marker for by_marker in GATE_BY_FILE.values() for marker in by_marker}
    assert markers, "gate map must reference at least one marker"
    for marker in sorted(markers):
        reason = gate_reason(marker)
        assert reason and "requires private" in reason, (
            f"marker {marker} has no explicit private-input reason"
        )


# ---------------------------------------------------------------------------
# HIGH remediation regression: per-test gates must report the ACTUALLY missing
# private input under a partial restoration, never a catch-all "any policy
# JSON" that lets a test run and fail on an absent v1/v2 file.
# ---------------------------------------------------------------------------


def _missing_names(reason: str) -> list[str]:
    """The private file basenames a gate reason names as missing."""
    return [
        name for name in (
            "ginzu-review-scope-v2.json", "ginzu-input-policy-v2.json",
            "ginzu-input-policy-v2.md", "ginzu-new-cell-inventory-v2.json",
            "ginzu-southwest-review-v2.json", "ginzu-review-manifest-v1.json",
            "ginzu-policy-review-packet-v1.md", "southwest-input-review.md",
            "ginzu-input-policy-v1.json", "ginzu-input-policy-v1.md",
            "ginzu-policy-proposed-review-only.xlsx",
            "ginzu-policy-proposed-review-only-manifest.json",
            "ginzu-policy-proposed-review-only-v2.xlsx",
            "ginzu-policy-proposed-review-only-v2-manifest.json",
            "RUNBOOK.md", "PROGRESS.md", "BACKLOG.md", "CHANGES.md",
            "MAINTENANCE_CHECKLIST.md",
        ) if name in reason
    ]


def test_partial_restoration_gates_named_missing_inputs(tmp_path, monkeypatch):
    """A partially restored clone (workbook + only the v1 policy JSON, no v2
    packet and no review artifacts) must gate the v1/v2 review tests with a
    reason naming the actual missing file.

    The state is simulated with monkeypatched PRIVATE_PATHS entries pointing at
    temporary synthetic files (gating checks presence only), so the original
    workbook is never copied.
    """
    from _private_inputs import GATE_BY_FILE, PRIVATE_PATHS, gate_reason

    # Phase 1: workbook + only the v1 input-policy JSON present.
    workbook = tmp_path / "fcffsimpleginzu.xlsx"
    workbook.write_text("synthetic placeholder, not the private workbook", encoding="utf-8")
    v1_policy = tmp_path / "docs" / "policy-drafts" / "ginzu-input-policy-v1.json"
    v1_policy.parent.mkdir(parents=True)
    v1_policy.write_text("{}", encoding="utf-8")
    monkeypatch.setitem(PRIVATE_PATHS, "workbook", workbook)
    monkeypatch.setitem(PRIVATE_PATHS, "draft_json_v1", v1_policy)

    # Every v2/review-artifact gate must fire and name a concrete missing file.
    v2_gates = {
        "needs_workbook_v2_packet", "needs_workbook_v2_packet_v1",
        "needs_workbook_v2_artifacts", "needs_workbook_v2_packet_v1_artifacts",
        "needs_workbook_v1_artifacts", "needs_scope_policy_v2_v1",
        "needs_v2_policy_v1", "needs_workbook_inventory",
        "needs_v2_baseline_packet", "needs_scope_southwest",
    }
    for marker in v2_gates:
        reason = gate_reason(marker)
        assert reason is not None, f"{marker} must gate under partial restoration"
        assert "requires private" in reason, reason
        assert _missing_names(reason), f"{marker} reason must name a missing file: {reason!r}"

    # The v1 review-copy generation still needs the v1 policy MD, which is
    # absent here -> the reason must name exactly that file.
    v1_reason = gate_reason("needs_workbook_v1_policy")
    assert v1_reason is not None and "ginzu-input-policy-v1.md" in v1_reason

    # The v1-note-only test only needs the v1 policy JSON, which IS present.
    assert gate_reason("needs_v1_policy_json") is None

    # Per-test mapping: every v2 review-copy test gates (skips) under this
    # partial restoration instead of running and failing on the missing packet.
    for marker, tests in GATE_BY_FILE["test_review_copy_v2.py"].items():
        assert gate_reason(marker) is not None, f"{marker} must gate v2 tests"
        assert tests

    # Phase 2: reverse partial restoration — only the v2 input-policy JSON is
    # restored; v1 policy and the workbook are absent.
    v2_policy = tmp_path / "v2only" / "ginzu-input-policy-v2.json"
    v2_policy.parent.mkdir(parents=True)
    v2_policy.write_text("{}", encoding="utf-8")
    monkeypatch.setitem(PRIVATE_PATHS, "input_policy_json_v2", v2_policy)
    monkeypatch.setitem(PRIVATE_PATHS, "workbook", tmp_path / "absent" / "fcffsimpleginzu.xlsx")
    monkeypatch.setitem(PRIVATE_PATHS, "draft_json_v1", tmp_path / "absent" / "ginzu-input-policy-v1.json")

    v1_missing = gate_reason("needs_v2_policy_v1")
    assert v1_missing is not None and "ginzu-input-policy-v1.json" in v1_missing
    workbook_missing = gate_reason("needs_workbook_v2_packet")
    assert workbook_missing is not None and "fcffsimpleginzu.xlsx" in workbook_missing


def test_partial_restoration_one_review_doc_keeps_foundation_gated(tmp_path, monkeypatch):
    """Restoring ONLY one private review doc must not enable the test_foundation
    review-doc family.

    The old any-of ``needs_private_review_docs`` gate fired as soon as ANY one
    review doc existed, so a partially restored clone (just
    docs/provisional-comment-review.md) enabled all three foundation tests even
    though each reads a different set of docs (AGENTS.md, docs/PROGRESS.md,
    docs/todo.md, ...) that were still missing.  Each foundation test now gates
    on the exact docs it reads, so a single restored doc must leave the whole
    family gated with the actually missing docs named.
    """
    import _private_inputs as _private
    from _private_inputs import GATE_BY_FILE, gate_reason

    # Phase 1: restore exactly one review doc from the old any-of family.
    one_doc = tmp_path / "docs" / "provisional-comment-review.md"
    one_doc.parent.mkdir(parents=True)
    one_doc.write_text("synthetic placeholder, not the private review doc", encoding="utf-8")
    monkeypatch.setattr(_private, "PRIVATE_REVIEW_DOCS", [one_doc])

    # The old any-of condition is satisfied by that single restored doc ...
    assert _private._any_review_doc_present()

    # ... but the foundation family gates on the exact docs each test reads.
    by_marker = GATE_BY_FILE["test_foundation.py"]
    assert by_marker, "test_foundation.py must gate its review-doc family"
    for marker, tests in by_marker.items():
        assert tests, f"{marker} must gate at least one foundation test"
        reason = gate_reason(marker)
        assert reason is not None, (
            f"{marker} must still gate after a one-doc partial restoration"
        )
        assert "requires private" in reason, reason
        # The reason names the docs this test actually needs, not the restored
        # single review doc.
        assert "provisional-comment-review.md" not in reason, reason

    # Phase 2: restoring only docs/PROGRESS.md — which test_progress_todo_schema
    # reads — still must not enable it, because it also reads docs/progress.md,
    # docs/todo.md and docs/BACKLOG.md.
    progress_doc = tmp_path / "docs2" / "PROGRESS.md"
    progress_doc.parent.mkdir(parents=True)
    progress_doc.write_text("synthetic placeholder, not the private status doc", encoding="utf-8")
    monkeypatch.setitem(_private.PRIVATE_PATHS, "progress_doc", progress_doc)

    reason = gate_reason("needs_foundation_progress_docs")
    assert reason is not None, "needs_foundation_progress_docs must gate with only PROGRESS.md restored"
    assert "PROGRESS.md at" not in reason, reason  # present now, so not a missing file
    for missing in ("progress.md", "todo.md", "BACKLOG.md"):
        assert missing in reason, (missing, reason)


def test_partial_restoration_one_review_doc_and_workbook_absence_keep_docs_gated(tmp_path, monkeypatch):
    """Restoring ONLY one private review doc must not enable any documentation
    test that reads other docs (or the workbook), and the workbook-gated test
    must stay gated on the absent workbook even when private docs exist.

    The old any-of ``needs_private_review_docs`` gate fired as soon as ANY one
    review doc existed, so a partially restored clone (just
    docs/provisional-comment-review.md) enabled every test in the
    test_autonomous_target_docs / test_detailed_documentation /
    test_provisional_documentation families even though each reads a different
    set of docs that were still missing.  Each of those tests now gates on the
    exact docs it reads (and the workbook-pin test on the workbook), so a
    single restored doc must leave the rest of the family gated with the
    actually missing docs named, and restoring the workbook must not enable any
    doc test.
    """
    import _private_inputs as _private
    from _private_inputs import GATE_BY_FILE, gate_reason

    docs_modules = (
        "test_autonomous_target_docs.py",
        "test_detailed_documentation.py",
        "test_provisional_documentation.py",
    )

    # Phase 1: restore exactly one private review doc from the old any-of
    # family; the other seven stay absent and the workbook stays absent.
    one_doc = tmp_path / "docs" / "provisional-comment-review.md"
    one_doc.parent.mkdir(parents=True)
    one_doc.write_text("synthetic placeholder, not the private review doc", encoding="utf-8")
    monkeypatch.setitem(_private.PRIVATE_PATHS, "provisional_comment_review_doc", one_doc)
    family = [one_doc] + [
        tmp_path / "absent" / f"review-doc-{i}.md" for i in range(7)
    ]
    monkeypatch.setattr(_private, "PRIVATE_REVIEW_DOCS", family)

    # The old any-of condition is satisfied by that single restored doc ...
    assert _private._any_review_doc_present()

    # ... but every documentation test stays gated on the exact docs (or the
    # workbook) it reads; a single restored doc never enables another test.
    for module in docs_modules:
        by_marker = GATE_BY_FILE[module]
        assert by_marker, f"{module} must gate its docs/workbook family"
        for marker, tests in by_marker.items():
            assert tests, f"{marker} must gate at least one test in {module}"
            reason = gate_reason(marker)
            assert reason is not None, (
                f"{marker} must still gate after a one-doc partial restoration"
            )
            assert "requires private" in reason, reason
            # The reason names the docs/workbook this test actually needs, not
            # the single restored review doc.
            assert "provisional-comment-review.md" not in reason, reason

    # The workbook-pin test uses the workbook marker, not a doc family, and
    # must stay gated on the absent workbook.
    assert "test_documentation_amendment_preserves_pinned_workbook" in GATE_BY_FILE[
        "test_autonomous_target_docs.py"]["needs_workbook"]
    workbook_reason = gate_reason("needs_workbook")
    assert workbook_reason is not None and "fcffsimpleginzu.xlsx" in workbook_reason

    # Phase 2: restore a synthetic workbook; the workbook gate clears, but the
    # doc gates remain (a restored workbook must never enable a doc test).
    synthetic_workbook = tmp_path / "wb" / "fcffsimpleginzu.xlsx"
    synthetic_workbook.parent.mkdir(parents=True)
    synthetic_workbook.write_bytes(b"synthetic placeholder, not the private workbook")
    monkeypatch.setitem(_private.PRIVATE_PATHS, "workbook", synthetic_workbook)
    assert gate_reason("needs_workbook") is None, \
        "workbook gate must clear when the workbook is restored"
    for module in docs_modules:
        for marker, tests in GATE_BY_FILE[module].items():
            if marker == "needs_workbook":
                continue
            reason = gate_reason(marker)
            assert reason is not None, (
                f"{marker} must still gate after restoring only the workbook"
            )


# ---------------------------------------------------------------------------
# MEDIUM remediation: the standalone verifier's hermetic environment must clear
# inherited AIRLINE_VALUATION_* overrides first and then set explicit hermetic
# SEC/private locators and strict flags.
# ---------------------------------------------------------------------------


def _load_verify_public_export():
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "verify_public_export", ROOT / "tools" / "verify_public_export.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_hermetic_env_drops_inherited_airline_overrides(tmp_path, monkeypatch):
    """_hermetic_env clears inherited AIRLINE_VALUATION_* overrides and then
    sets explicit hermetic SEC/private locators and strict flags, so the owner's
    fixture dir and strict flags never leak into a hermetic checkout."""
    verify = _load_verify_public_export()

    # The parent environment carries the owner's SEC fixture locator and strict
    # flags; the hermetic builder must not inherit any of them.
    monkeypatch.setenv("AIRLINE_VALUATION_SEC_FIXTURE_DIR", "/owner/private/sec-fixtures")
    monkeypatch.setenv("AIRLINE_VALUATION_REQUIRE_SEC_FIXTURES", "1")
    monkeypatch.setenv("AIRLINE_VALUATION_REQUIRE_PRIVATE_INPUTS", "1")

    root = tmp_path / "checkout"
    root.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    fixtures = tmp_path / "fixtures"
    fixtures.mkdir()
    env = verify._hermetic_env(root, home, fixtures)

    # Base hermetic environment is in place.
    assert env["HOME"] == str(home)
    assert env["PYTHONPATH"] == str(root / "src")
    # Explicit hermetic SEC locator wins over the inherited owner locator.
    assert env["AIRLINE_VALUATION_SEC_FIXTURE_DIR"] == str(fixtures / "sec")
    # Strict flags are explicitly cleared for the hermetic run.
    assert env["AIRLINE_VALUATION_REQUIRE_SEC_FIXTURES"] == ""
    assert env["AIRLINE_VALUATION_REQUIRE_PRIVATE_INPUTS"] == ""
