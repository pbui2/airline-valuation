import hashlib
import json
import shutil
from pathlib import Path
from collections import Counter

import pytest

from airline_valuation import cli, filing_report
from airline_valuation.filing_archive import import_filings
from airline_valuation.filing_eligibility import accounting_eligibility_test_fixture
from airline_valuation.filing_ixbrl import resolve_locator
from airline_valuation.filing_registry import FILINGS
from airline_valuation.filing_report import (analyze_archive, analyze_archive_test_fixture,
                                             verify_test_only_lease_artifact)
from conftest import sec_fixture_files


def inputs(tmp_path, names=("southwest", "delta")):
    filenames = [
        FILINGS[name]["artifacts"][kind]["filename"]
        for name in names
        for kind in ("submissions", "index", "primary")
    ]
    files = sec_fixture_files(*filenames)
    meta, docs = tmp_path / "metadata", tmp_path / "documents"
    meta.mkdir(parents=True); docs.mkdir(parents=True)
    for name in names:
        for kind in ("submissions", "index"):
            filename = FILINGS[name]["artifacts"][kind]["filename"]
            shutil.copyfile(files[filename], meta / filename)
        filename = FILINGS[name]["artifacts"]["primary"]["filename"]
        shutil.copyfile(files[filename], docs / filename)
    return meta, docs


def archive(tmp_path, names=("southwest", "delta")):
    meta, docs = inputs(tmp_path, names)
    return import_filings(meta, docs, tmp_path / "archive")


def test_filing_analyze_cli_end_to_end_two_issuers(tmp_path, monkeypatch):
    source = archive(tmp_path)
    output = tmp_path / "report"
    parsed_issuers = Counter()
    original_parse_filing = filing_report.parse_filing

    def counted_parse_filing(raw, *, source_id=None, **kwargs):
        parsed_issuers[source_id] += 1
        return original_parse_filing(raw, source_id=source_id, **kwargs)

    monkeypatch.setattr(filing_report, "parse_filing", counted_parse_filing)
    assert cli.main(["filing-analyze", "--archive-dir", str(source), "--output-dir", str(output)]) == 0
    assert parsed_issuers == Counter({"southwest": 1, "delta": 1})
    sw = json.loads((output / "southwest-report.json").read_text())
    delta = json.loads((output / "delta-report.json").read_text())
    assert sw["source_format"] == delta["source_format"] == "html_inline_xbrl"
    assert sw["reconciliations"]["cash_and_cash_equivalents"]["status"] == "reconciled"
    assert sw["reconciliations"]["debt"]["status"] == "abstain_debt_definitions_unresolved"
    assert sw["reconciliations"]["short_term_investments"]["status"].startswith("abstain")
    assert delta["status"].startswith("extraction-only") and delta["reconciliations"] == {}
    assert sw["source"]["rights"] == sw["source"]["first_public_availability"] == "unknown"
    assert all(not x["workbook_promotion"] for x in (sw, delta))
    assert (output / "ledger.json").is_file() and (output / "run-state.json").is_file()
    assert not list(output.glob("*.xlsx"))


def test_filing_archive_rehydrate_locator_and_lease_bytes(analyzed_filing_archive):
    source, output = analyzed_filing_archive
    obs = json.loads((output / "southwest-observations.json").read_text())
    raw = (source / "southwest-luv-20251231.htm").read_bytes()
    report = json.loads((output / "southwest-report.json").read_text())
    lease = report["reconciliations"]["lease_liabilities"]
    assert lease["status"] == "reconciled"
    digest = hashlib.sha256(raw).hexdigest()
    assert lease["operating_total"]["locator"]["sha256"] == digest
    assert resolve_locator(raw, lease["operating_total"]["locator"])
    assert obs["source_sha256"] == digest
    assert lease["units"] == "USD millions"
    assert lease["equations"] == {"operating": "312 + 768 = 1080", "finance": "25 + 53 = 78"}
    for row in [lease["operating_total"], lease["finance_total"], *lease["component_evidence"]]:
        assert row["locator"]["sha256"] == digest
        assert resolve_locator(raw, row["locator"])
        assert row["context_id"] == "c-4"
        assert row["unit"] == "iso4217:USD"


def test_html_xbrl_report_without_workbook_promotion(analyzed_filing_archive):
    source, output = analyzed_filing_archive
    obs = json.loads((output / "delta-observations.json").read_text())
    assert obs["candidates"] and obs["contexts"] and obs["units"]
    assert obs["workbook_promotion"] is False
    assert not (output / "prepared.xlsx").exists()


def test_filing_report_test_only_permission_isolation(tmp_path):
    source = archive(tmp_path)
    result = analyze_archive_test_fixture(source, tmp_path / "TEST-ONLY-test-report")
    report = json.loads((result / "southwest-report.json").read_text())
    delta_report = json.loads((result / "delta-report.json").read_text())
    trace = report["test_only_permission_trace"]
    assert trace["eligible"] is True and trace["fixture_only"] is True
    assert trace["production_source_clearance"] is False
    assert trace["report_status"] == "TEST ONLY"
    assert "TEST ONLY" in trace["label"]
    assert trace["terms_sha256"] and trace["availability_sha256"]
    assert report["source"]["rights"] == report["source"]["first_public_availability"] == "unknown"
    artifact_ref = report["test_only_lease_artifact"]
    artifact_bytes = (result / artifact_ref["path"]).read_bytes()
    assert hashlib.sha256(artifact_bytes).hexdigest() == artifact_ref["sha256"]
    assert artifact_ref["source_sha256"] == report["source"]["sha256"]
    assert artifact_ref["verified_against_rehydrated_archive"] is True
    assert verify_test_only_lease_artifact(source, artifact_bytes, artifact_ref["sha256"])["fixture_only"] is True
    with pytest.raises(ValueError):
        verify_test_only_lease_artifact(source, artifact_bytes + b"tamper", artifact_ref["sha256"])
    assert "test_only_permission_trace" not in delta_report
    help_text = cli.build_parser().format_help()
    assert "filing-analyze-test-fixture" in help_text
    assert "--test-permission" not in help_text and "--permission-record" not in help_text
    assert not hasattr(cli.build_parser()._subparsers._group_actions[0].choices["filing-analyze"], "test_fixture")
    assert accounting_eligibility_test_fixture("caller-claims-allowed")["eligible"] is False
    production_parser = cli.build_parser()._subparsers._group_actions[0].choices["filing-analyze"]
    test_parser = cli.build_parser()._subparsers._group_actions[0].choices["filing-analyze-test-fixture"]
    assert {action.dest for action in production_parser._actions} == {"help", "archive_dir", "output_dir"}
    assert {action.dest for action in test_parser._actions} == {"help", "archive_dir", "output_dir"}
    source = archive(tmp_path / "bad-output")
    with pytest.raises(ValueError, match="TEST-ONLY"):
        analyze_archive_test_fixture(source, tmp_path / "ambiguous-report")


@pytest.mark.parametrize("failure", ["reconciliation", "verification"])
def test_filing_report_test_only_lease_artifact_fails_closed(tmp_path, monkeypatch, failure):
    source = archive(tmp_path)
    if failure == "reconciliation":
        original_reconcile = filing_report._reconcile_verified_parse

        def unreconciled(raw, parsed, issuer):
            result = original_reconcile(raw, parsed, issuer)
            if issuer == "southwest":
                result["reconciliations"]["lease_liabilities"]["status"] = "abstain_test"
            return result

        monkeypatch.setattr(filing_report, "_reconcile_verified_parse", unreconciled)
    else:
        monkeypatch.setattr(filing_report, "verify_test_only_lease_artifact", lambda *args: {})

    output = analyze_archive_test_fixture(source, tmp_path / "TEST-ONLY-test-report")
    report = json.loads((output / "southwest-report.json").read_text())
    assert "test_only_lease_artifact" not in report
    assert report["test_only_permission_trace"]["eligible"] is False
    assert not list(output.glob("*-test-only-lease-reconciliation.json"))
    assert report["reconciliations"].get("lease_liabilities", {}).get("status") != "reconciled" if failure == "reconciliation" else report["reconciliations"]["lease_liabilities"]["status"] == "reconciled"


def test_filing_report_lineage_and_status(analyzed_filing_archive):
    source, output = analyzed_filing_archive
    state = json.loads((output / "run-state.json").read_text())
    report = json.loads((output / "southwest-report.json").read_text())
    obs = json.loads((output / "southwest-observations.json").read_text())
    assert state["archive_manifest_sha256"] == hashlib.sha256((source / "manifest.json").read_bytes()).hexdigest()
    assert state["issuers"]["southwest"]["source_sha256"] == obs["source_sha256"]
    assert state["issuers"]["southwest"]["report_sha256"] == hashlib.sha256((output / "southwest-report.json").read_bytes()).hexdigest()
    assert report["eligibility"]["status"] == "unknown"
    assert report["eligibility"]["first_public_availability"] == "unknown"
    assert report["source"]["retrieved_at"] is None


def test_existing_workflow_source_isolation(analyzed_filing_archive):
    source, output = analyzed_filing_archive
    assert "detailed-analyze" in cli.build_parser().format_help()
    assert "filing-analyze" in cli.build_parser().format_help()
    # Filing reports are separate outputs; the existing detailed decision builder
    # still accepts only its explicit evidence-bundle interface.
    assert not hasattr(cli, "cmd_filing_analyze_decisions")
    assert json.loads((output / "southwest-report.json").read_text())["workbook_promotion"] is False
    assert not (output / "prepared.xlsx").exists()


def test_filing_analyze_rejects_modified_symlink_and_partial_archive(tmp_path):
    source = archive(tmp_path, ("southwest",))
    (source / "southwest-luv-20251231.htm").write_bytes(b"modified")
    with pytest.raises(ValueError):
        analyze_archive(source, tmp_path / "bad-report")
    source_link = archive(tmp_path / "third", ("southwest",))
    artifact = source_link / "southwest-luv-20251231.htm"
    artifact.unlink()
    fixture = FILINGS["southwest"]["artifacts"]["primary"]["filename"]
    artifact.symlink_to(sec_fixture_files(fixture)[fixture])
    with pytest.raises(ValueError):
        analyze_archive(source_link, tmp_path / "symlink-report")
    source2 = archive(tmp_path / "second", ("southwest",))
    manifest = json.loads((source2 / "manifest.json").read_text())
    del manifest["filings"]["southwest"]["artifacts"]["index"]
    (source2 / "manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(ValueError):
        analyze_archive(source2, tmp_path / "partial-report")


def test_public_offline_import_then_production_and_test_only_analyze(tmp_path, capsys):
    meta, docs = inputs(tmp_path, ("southwest", "delta"))
    imported = tmp_path / "imported"
    assert cli.main(["filing-import", "--metadata-dir", str(meta), "--document-dir", str(docs), "--output-dir", str(imported)]) == 0
    production = tmp_path / "production-report"
    assert cli.main(["filing-analyze", "--archive-dir", str(imported), "--output-dir", str(production)]) == 0
    for name in ("southwest", "delta"):
        assert json.loads((production / f"{name}-report.json").read_text())["source"]["source_bytes_verified"] is True

    test_output = tmp_path / "TEST-ONLY-filing-report"
    assert cli.main(["filing-analyze-test-fixture", "--archive-dir", str(imported), "--output-dir", str(test_output)]) == 0
    assert "TEST ONLY" in capsys.readouterr().out
    sw = json.loads((test_output / "southwest-report.json").read_text())
    delta = json.loads((test_output / "delta-report.json").read_text())
    trace = sw["test_only_permission_trace"]
    assert sw["test_only_status"] == trace["report_status"] == "TEST ONLY"
    assert trace["eligible"] is True and trace["fixture_only"] is True
    assert trace["production_source_clearance"] is False
    assert trace["terms_sha256"] and trace["availability_sha256"]
    assert sw["source"]["rights"] == sw["source"]["first_public_availability"] == "unknown"
    lease = sw["reconciliations"]["lease_liabilities"]
    raw = (imported / "southwest-luv-20251231.htm").read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    assert lease["status"] == "reconciled" and lease["units"] == "USD millions"
    assert lease["operating_total"]["context_id"] == "c-4"
    assert lease["operating_total"]["locator"]["sha256"] == digest
    assert resolve_locator(raw, lease["operating_total"]["locator"])
    assert delta["status"].startswith("extraction-only")
    assert "test_only_permission_trace" not in delta
    assert not list(test_output.glob("*.xlsx")) and not list(production.glob("*.xlsx"))
    assert json.loads((production / "southwest-report.json").read_text())["source"]["rights"] == "unknown"
    assert "test_only_permission_trace" not in json.loads((production / "southwest-report.json").read_text())
