"""Acceptance checks for evidence readiness and workbook/CLI safeguards.

These automated checks inspect workbook structure and manifest rules only; they
never claim to open Excel or satisfy the required human review gate.
"""
from copy import deepcopy
import hashlib
import json
from pathlib import Path

import pytest

from airline_valuation import cli
from airline_valuation.manifest import assess_readiness, validate_manifest
from airline_valuation.ooxml_writer import verify_prepared_delta
from airline_valuation.sec import SECClient
from airline_valuation.workbook import Workbook

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "examples" / "fy2025-manifest.json"
TEMPLATE = ROOT / "fcffsimpleginzu.xlsx"
CONTRACT = ROOT / "src" / "airline_valuation" / "contract.json"


def load_manifest():
    return json.loads(EXAMPLE.read_text(encoding="utf-8"))


def synthetic_approved_manifest():
    """Test-only data for workflow reachability, not airline financial assertions."""
    manifest = load_manifest()
    contract = json.loads(CONTRACT.read_text(encoding="utf-8"))
    unit_overrides = {
        "Input sheet!B11": "USD millions",
        "Input sheet!B12": "USD millions",
        "Input sheet!B14": "USD millions",
        "Input sheet!B15": "USD millions",
        "Input sheet!B21": "millions of shares",
        "Input sheet!B22": "currency/share",
        "Input sheet!B23": "fraction 0-1; B23 validation 0 to 1",
        "Input sheet!B24": "fraction",
        "Input sheet!B26": "fraction",
        "Input sheet!B29": "fraction",
        "Input sheet!B30": "years",
        "Input sheet!B31": "ratio",
        "Input sheet!B34": "fraction",
        "Cost of capital worksheet!B11": "I will input",
        "Cost of capital worksheet!B12": "fraction",
    }
    synthetic_values = {
        "Input sheet!B11": "1", "Input sheet!B12": "0", "Input sheet!B14": "1", "Input sheet!B15": "0",
        "Input sheet!B21": "491.31799", "Input sheet!B22": "1", "Input sheet!B23": "0", "Input sheet!B24": "0",
        "Input sheet!B26": "0", "Input sheet!B29": "0", "Input sheet!B30": "1", "Input sheet!B31": "1",
        "Input sheet!B34": "0.01", "Cost of capital worksheet!B11": "I will input", "Cost of capital worksheet!B12": "0.1",
    }
    digest = hashlib.sha256(b"synthetic test bytes only").hexdigest()
    acquired_at = "2026-02-07T10:00:00Z"
    for filing in manifest["filings"]:
        filing.update(status="ready", sha256=digest,
                      acquisition={"sha256": digest, "retrieved_at": acquired_at})
    manifest["facts"] = []
    manifest["market_observations"] = []
    manifest["analyst_assumptions"] = []
    for index, (cell, value) in enumerate(synthetic_values.items()):
        meta = contract["input_metadata"][cell]
        item = {"id": f"synthetic_test_{index}", "value": value, "unit": unit_overrides[cell],
                "status": "approved", "workbook_cell": cell,
                "zero_semantics": "explicit_zero" if value == "0" else None,
                "rationale": "Synthetic workflow test value; not a Southwest financial claim.",
                "author": "Test fixture", "approval_date": "2026-02-07"}
        if cell == "Input sheet!B21":
            item.update(as_of="2026-02-03T16:00:00-05:00", published_at="2026-02-03T16:00:00-05:00", period="FY2025", tag="SyntheticTestTag")
            item["source"] = {"url": "https://www.sec.gov/Archives/edgar/data/92380/000009238026000004/luv-20251231.htm",
                              "accession": "0000092380-26-000004", "accessed_at": acquired_at,
                              "description": "Synthetic test evidence", "sha256": digest,
                              "acquisition": {"sha256": digest, "retrieved_at": acquired_at}}
        else:
            item.update(as_of="2026-02-05T12:00:00-05:00", published_at="2026-02-05T12:00:00-05:00")
            item["source"] = {"url": "https://www.sec.gov/Archives/edgar/data/92380/000009238026000004/luv-20251231.htm",
                              "accession": "0000092380-26-000004", "accessed_at": acquired_at,
                              "description": "Synthetic test evidence only", "sha256": digest,
                              "acquisition": {"sha256": digest, "retrieved_at": acquired_at}}
            item.update(period="FY2025", tag="SyntheticTestTag")
        if cell == "Cost of capital worksheet!B11":
            item.pop("source", None)
            item["status"] = "approved"
            manifest["analyst_assumptions"].append(item)
        else:
            manifest["facts"].append(item)
    manifest["required_cells"] = list(synthetic_values)
    manifest["approval"] = {"status": "approved"}
    manifest["analyst_decisions"] = []
    manifest["review_packet"]["required_sections"] = [{"name": "synthetic test", "status": "complete"}]
    return manifest


def test_prepare_populates_approved_inputs(tmp_path, capsys):
    manifest = synthetic_approved_manifest()
    b21 = next(item for item in manifest["facts"] if item.get("workbook_cell") == "Input sheet!B21")
    assert b21["value"] == "491.31799" and b21["unit"] == "millions of shares"
    assert validate_manifest(manifest).valid, validate_manifest(manifest).errors
    assert assess_readiness(manifest).ready, assess_readiness(manifest).blockers
    path = tmp_path / "synthetic-manifest.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    output = tmp_path / "run"
    template_sha = cli.sha256(TEMPLATE)
    result = cli.main(["prepare", "--manifest", str(path), "--output-dir", str(output)])
    assert result == 0, capsys.readouterr().err

    prepared = output / "prepared.xlsx"
    normalized = {}
    for collection in ("facts", "market_observations", "analyst_assumptions"):
        for item in manifest[collection]:
            refs = item.get("workbook_cells", item.get("workbook_cell", []))
            refs = [refs] if isinstance(refs, str) else refs
            for ref in refs:
                value, _unit = cli._normalize_mapped_entry(
                    ref, item, json.loads(CONTRACT.read_text(encoding="utf-8"))["input_metadata"][ref]
                )
                normalized[ref] = str(value)
    delta = verify_prepared_delta(TEMPLATE, prepared, normalized)
    assert delta == {"valid": True, "changed_cells": sorted(normalized)}

    with Workbook(prepared, CONTRACT) as workbook:
        assert str(workbook.read_input("B21")) == "491.31799"
        assert workbook.read_input("B11") == 1
        assert workbook.read_input("B23") == 0
        assert workbook.read_input("B24") == 0
        assert workbook.read_input("B34") == 0.01
        assert workbook.read_input("B11", "Cost of capital worksheet") == "I will input"
        assert workbook.read_input("B12", "Cost of capital worksheet") == 0.1

    state = json.loads((output / cli.RUN_STATE).read_text(encoding="utf-8"))
    assert state["template_sha256"] == template_sha == cli.sha256(TEMPLATE)
    assert (output / "checklist.txt").is_file() and (output / "checklist.txt").stat().st_size > 0
    ledger_lines = (output / "ledger.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(ledger_lines) == 1
    ledger = json.loads(ledger_lines[0])
    assert ledger["items"]
    assert {entry["cell"] for entry in ledger["items"]} == set(normalized)

    # A prepared workbook is not an Excel-saved or reviewer-attested workbook.
    assert cli.main(["inspect", "--run-dir", str(output), "--workbook", str(prepared)]) == 2
    report = json.loads((output / "inspect-report.json").read_text(encoding="utf-8"))
    assert report["status"] == "unverified"
    assert report["attestation_outcome"] == "missing-or-unreadable"
    assert any("missing attestation" in issue or "attestation" in issue for issue in report["issues"])


def test_prepare_converts_whole_share_source_to_millions(tmp_path, capsys):
    manifest = synthetic_approved_manifest()
    b21 = next(item for item in manifest["facts"] if item.get("workbook_cell") == "Input sheet!B21")
    b21["value"] = "491317990"
    b21["unit"] = "shares"
    path = tmp_path / "whole-share-manifest.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    output = tmp_path / "whole-share-run"
    assert cli.main(["prepare", "--manifest", str(path), "--output-dir", str(output)]) == 0, capsys.readouterr().err
    with Workbook(output / "prepared.xlsx", CONTRACT) as workbook:
        assert str(workbook.read_input("B21")) == "491.31799"
    ledger = json.loads((output / "ledger.jsonl").read_text(encoding="utf-8").splitlines()[0])
    item = next(entry for entry in ledger["items"] if entry["cell"] == "Input sheet!B21")
    assert item["raw_value"] == "491317990"
    assert item["normalized_value"] == "491.31799"
    assert item["normalized_unit"] == "millions of shares"


def test_acceptance_pinned_cutoff_and_source_times():
    manifest = load_manifest()
    assert manifest["cutoff"] == "2026-02-06T23:59:59-05:00"
    assert manifest["filings"][0]["filed_at"] == "2026-02-05T16:07:52-05:00"
    treasury = next(x for x in manifest["market_observations"] if x["id"].startswith("risk_free_rate"))
    assert treasury["observation_date"] == "2026-02-06"
    assert treasury["value"] == "0.0422"
    assert treasury["as_of"] is None and treasury["published_at"] is None
    price = next(x for x in manifest["market_observations"] if x["id"].startswith("share_price"))
    assert price["value"] == "54.26"
    assert price["as_of"] == "2026-02-06T16:00:00-05:00"
    assert price["status"] == "pending"
    assert not assess_readiness(manifest).ready


def test_acceptance_original_filing_and_amendment_scope():
    manifest = load_manifest()
    original, amendment = manifest["filings"]
    assert original["accession"] == "0000092380-26-000004"
    assert original["amendment_scope"] == "original"
    assert amendment["accession"] == "0000092380-26-000006"
    assert amendment["amendment_scope"] == "exhibits_only"
    fact = manifest["facts"][0]
    fact["source"]["accession"] = amendment["accession"]
    fact["status"] = "ready"
    assert any("original 10-K accession" in e for e in validate_manifest(manifest).errors)


def test_acceptance_invalid_share_count_is_rejected():
    manifest = load_manifest()
    shares = next(x for x in manifest["market_observations"] if x["id"].startswith("shares_"))
    shares["value"] = "0"
    assert any("positive" in e for e in validate_manifest(manifest).errors)


def test_acceptance_missing_financial_fact_blocks_readiness():
    manifest = load_manifest()
    fact = manifest["facts"][0]
    fact.update(value=None, status="pending")
    readiness = assess_readiness(manifest)
    assert not readiness.ready
    assert any("financial_facts is pending or unknown" in blocker for blocker in readiness.blockers)


def test_acceptance_no_lookahead_rejects_post_cutoff_publication():
    manifest = load_manifest()
    fact = manifest["facts"][0]
    fact.update(value="100", status="ready", as_of="2026-02-05T16:00:00-05:00",
                published_at="2026-02-07T09:00:00-05:00", tag="ExampleFact")
    assert any("published_at is after cutoff" in e for e in validate_manifest(manifest).errors)


def test_acceptance_review_pending_and_prepare_blocked(tmp_path, capsys):
    manifest_path = EXAMPLE
    review_dir = tmp_path / "review"
    assert cli.main(["review", "--manifest", str(manifest_path), "--output-dir", str(review_dir)]) == 0
    packet = json.loads((review_dir / "review-packet.json").read_text(encoding="utf-8"))
    assert packet["status"] == "not ready"
    capsys.readouterr()

    prepare_dir = tmp_path / "prepare"
    result = cli.main(["prepare", "--manifest", str(manifest_path), "--output-dir", str(prepare_dir)])
    assert result == 2
    assert "PREPARE BLOCKED" in capsys.readouterr().err
    assert not (prepare_dir / "prepared.xlsx").exists()


def test_acceptance_manifest_validation_never_uses_network(monkeypatch, capsys):
    def forbidden_network(*args, **kwargs):
        raise AssertionError("offline validation attempted network access")

    monkeypatch.setattr("urllib.request.urlopen", forbidden_network)
    monkeypatch.setattr("airline_valuation.sec.urlopen", forbidden_network)
    client = SECClient("AirlineValuation test@example.com", opener=forbidden_network)
    assert client is not None  # SEC access is explicit opt-in, not a validation side effect.
    assert cli.main(["validate", "--manifest", str(EXAMPLE)]) == 2
    assert "NOT READY" in capsys.readouterr().out


def test_acceptance_contract_lookup_mode_is_blocked():
    with Workbook(TEMPLATE, CONTRACT) as workbook:
        status = workbook.branch_status()
    assert not status["ready"]
    assert status["unsupported_branches"] == ["Cost of capital worksheet!B11"]


def test_acceptance_finite_template_cache_without_signoff_stays_unverified(tmp_path):
    """Plausible cached values and a template copy do not replace Excel signoff."""
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    manifest = EXAMPLE
    saved_copy = tmp_path / "saved-template-copy.xlsx"
    saved_copy.write_bytes(TEMPLATE.read_bytes())
    (run_dir / cli.RUN_STATE).write_text(json.dumps({
        "manifest_path": str(manifest),
        "manifest_sha256": cli.sha256(manifest),
        "prepared_path": str(TEMPLATE),
        "template_path": str(TEMPLATE),
        "template_sha256": cli.sha256(TEMPLATE),
        "prepared_sha256": cli.sha256(TEMPLATE),
    }), encoding="utf-8")

    assert cli.main(["inspect", "--run-dir", str(run_dir), "--workbook", str(saved_copy)]) == 2
    report = json.loads((run_dir / "inspect-report.json").read_text(encoding="utf-8"))
    assert report["status"] == "unverified"
    assert report["attestation_outcome"] == "missing-or-unreadable"
    assert any("attestation" in issue and "missing file" in issue for issue in report["issues"])
    assert "No valuation is independently calculated or validated." in report["limitations"]


def _negative_inspect_run(tmp_path):
    """Create synthetic negative-test inputs; declaration is not a real signature."""
    manifest = synthetic_approved_manifest()
    manifest_path = tmp_path / "negative-test-manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    run_dir = tmp_path / "negative-test-run"
    run_dir.mkdir()
    contract = json.loads(CONTRACT.read_text(encoding="utf-8"))
    entries = {}
    ledger_items = []
    for collection in ("facts", "market_observations", "analyst_assumptions"):
        for item in manifest[collection]:
            refs = item.get("workbook_cells", item.get("workbook_cell", []))
            refs = [refs] if isinstance(refs, str) else refs
            for ref in refs:
                value, unit = cli._normalize_mapped_entry(ref, item, contract["input_metadata"][ref])
                entries[ref] = str(value)
                ledger_items.append({"cell": ref, "raw_value": item["value"], "raw_unit": item["unit"], "normalized_value": str(value), "normalized_unit": unit})
    prepared = run_dir / "prepared.xlsx"
    from airline_valuation.ooxml_writer import write_prepared
    write_prepared(TEMPLATE, prepared, entries)
    saved = tmp_path / "negative-test-saved.xlsx"
    saved.write_bytes(prepared.read_bytes())
    state = {"schema": "airline-valuation-run-state/1", "manifest_path": str(manifest_path),
             "manifest_sha256": cli.sha256(manifest_path), "prepared_path": str(prepared),
             "prepared_sha256": cli.sha256(prepared), "template_path": str(TEMPLATE),
             "template_sha256": cli.sha256(TEMPLATE)}
    (run_dir / cli.RUN_STATE).write_text(json.dumps(state), encoding="utf-8")
    (run_dir / "ledger.jsonl").write_text(json.dumps({"manifest_sha256": state["manifest_sha256"], "items": ledger_items}) + "\\n", encoding="utf-8")
    # Hash-bound signed-looking fields are synthetic test data and must never be
    # treated as evidence of a real reviewer or as a positive-gate assertion.
    attestation = {
        "schema": cli.ATTESTATION_SCHEMA, "manifest_sha256": state["manifest_sha256"],
        "prepared_sha256": state["prepared_sha256"], "saved_sha256": cli.sha256(saved),
        "excel_version": "synthetic negative-test fixture", "calculation_mode": "Automatic",
        "full_recalculation": True, "saved": True, "reopened": True,
        "iteration_enabled": False, "iteration_count": None, "iteration_maximum_change": None,
        "signed_by": "synthetic negative-test fixture only", "signed_date": "2026-03-01",
        "signature": "NOT A REAL SIGNATURE; negative test fixture only",
    }
    (run_dir / cli.ATTESTATION).write_text(json.dumps(attestation), encoding="utf-8")
    return run_dir, prepared, saved, manifest


def _rewrite_xlsx(source_path, destination_path, mutation):
    import zipfile
    with zipfile.ZipFile(source_path) as source, zipfile.ZipFile(destination_path, "w", zipfile.ZIP_DEFLATED) as changed:
        for info in source.infolist():
            data = source.read(info.filename)
            data = mutation(info.filename, data)
            changed.writestr(info, data)


def _assert_negative_inspect(run_dir, saved, capsys):
    assert cli.main(["inspect", "--run-dir", str(run_dir), "--workbook", str(saved)]) == 2
    report = json.loads((run_dir / "inspect-report.json").read_text(encoding="utf-8"))
    assert report["status"] == "unverified"
    assert json.loads((run_dir / cli.RUN_STATE).read_text(encoding="utf-8"))["outcome"] == "unverified"
    return report


def test_inspect_synthetic_negative_saved_input_mutation_is_unverified(tmp_path, capsys):
    run_dir, prepared, saved, manifest = _negative_inspect_run(tmp_path)
    contract = json.loads(CONTRACT.read_text(encoding="utf-8"))
    ref = "Input sheet!B11"
    item = next(item for item in manifest["facts"] if item.get("workbook_cell") == ref)
    original, _unit = cli._normalize_mapped_entry(ref, item, contract["input_metadata"][ref])

    def mutate_input(part, data):
        if part == "xl/worksheets/sheet1.xml":
            token = f">{original}</v>".encode()
            assert token in data
            return data.replace(token, b">987654</v>", 1)
        return data

    _rewrite_xlsx(prepared, saved, mutate_input)
    report = _assert_negative_inspect(run_dir, saved, capsys)
    assert any("saved input mismatch" in issue and ref in issue for issue in report["issues"])


def test_inspect_synthetic_negative_feature_or_formula_mutation_is_unverified(tmp_path, capsys):
    run_dir, prepared, saved, _manifest = _negative_inspect_run(tmp_path)

    def mutate_formula(part, data):
        if part == "xl/worksheets/sheet2.xml":
            assert b"B31/B32" in data
            return data.replace(b"B31/B32", b"B31/B33", 1)
        return data

    _rewrite_xlsx(prepared, saved, mutate_formula)
    report = _assert_negative_inspect(run_dir, saved, capsys)
    assert any("structure invalid" in issue or "formula" in issue.lower() for issue in report["issues"])


@pytest.mark.parametrize("cached_value,expected_issue", [(None, "required output missing/non-finite"), ("#VALUE!", "cached Excel"), ("NaN", "required output missing/non-finite")])
def test_inspect_synthetic_negative_missing_error_nonfinite_cached_output_is_unverified(tmp_path, capsys, cached_value, expected_issue):
    run_dir, prepared, saved, _manifest = _negative_inspect_run(tmp_path)
    contract = json.loads(CONTRACT.read_text(encoding="utf-8"))
    ref = contract["active_outputs_by_branch"]["direct_input"][0]["cell"]
    sheet, cell = ref.rsplit("!", 1)
    sheet_part = contract["sheet_metadata"][sheet]["part"]

    def mutate_cached_output(part, data):
        if part != sheet_part:
            return data
        import re
        cell_pattern = re.compile(
            rb'<c\b(?=[^>]*\br="' + re.escape(cell.encode()) + rb'")[^>]*>.*?</c\s*>',
            re.DOTALL,
        )
        match = cell_pattern.search(data)
        assert match is not None, f"cell {cell} is absent from {part}"
        cell_xml = match.group(0)
        formula_match = re.search(rb"<f(?:\s[^>]*)?>.*?</f\s*>", cell_xml, re.DOTALL)
        assert formula_match is not None, f"cell {cell} must retain its formula"
        formula = formula_match.group(0)
        cache_pattern = re.compile(rb"<v(?:\s[^>]*)?>.*?</v\s*>|<v(?:\s[^>]*)?\s*/>", re.DOTALL)
        if cached_value is None:
            changed_cell, count = cache_pattern.subn(b"", cell_xml, count=1)
            if count == 0:
                changed_cell = cell_xml
        else:
            replacement = b"<v>" + cached_value.encode() + b"</v>"
            changed_cell, count = cache_pattern.subn(replacement, cell_xml, count=1)
            if count == 0:
                changed_cell, count = re.subn(rb"</f\s*>", lambda found: found.group(0) + replacement, cell_xml, count=1)
                assert count == 1, f"cell {cell} has no formula end tag for cache insertion"
        assert formula in changed_cell, f"cache mutation changed formula in {cell}"
        changed = data[:match.start()] + changed_cell + data[match.end():]
        return changed

    _rewrite_xlsx(prepared, saved, mutate_cached_output)
    report = _assert_negative_inspect(run_dir, saved, capsys)
    if cached_value == "#VALUE!":
        assert ref in report["branch_status"]["cached_error_outputs"]
        assert any("cached_error_outputs" in issue and ref in issue for issue in report["issues"])
    else:
        assert any(expected_issue in issue and ref in issue for issue in report["issues"])
    assert not any("structure invalid" in issue or "authorized delta invalid" in issue for issue in report["issues"])


def test_acceptance_protected_formula_mutation_is_rejected(tmp_path):
    import zipfile

    target = tmp_path / "mutated.xlsx"
    with zipfile.ZipFile(TEMPLATE) as source, zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as changed:
        for info in source.infolist():
            data = source.read(info.filename)
            if info.filename == "xl/worksheets/sheet2.xml":
                assert b"B31/B32" in data
                data = data.replace(b"B31/B32", b"B31/B33", 1)
            changed.writestr(info, data)
    with Workbook(target, CONTRACT) as workbook, pytest.raises(ValueError, match="formula"):
        workbook.verify_structure()
