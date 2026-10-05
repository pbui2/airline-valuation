from __future__ import annotations

import hashlib
import json
from pathlib import Path
import zipfile

from airline_valuation import cli

ROOT = Path(__file__).resolve().parents[1]


def _guarded_formula_copy(source: Path, dest: Path) -> None:
    with zipfile.ZipFile(source) as original, zipfile.ZipFile(dest, "w") as changed:
        for info in original.infolist():
            data = original.read(info.filename)
            if info.filename == "xl/worksheets/sheet10.xml":
                assert b"<f>B50/B49</f>" in data
                data = data.replace(b"<f>B50/B49</f>", b"<f>IF(B48=0,0,B50/B49)</f>", 1)
            changed.writestr(info, data)


def test_inspect_rejects_provisional_schema_before_mutating_state(tmp_path: Path, capsys):
    run_dir = tmp_path / "provisional-run"
    run_dir.mkdir()
    state_path = run_dir / cli.RUN_STATE
    raw = json.dumps({
        "schema": "airline-valuation-provisional-run-state/1",
        "outcome": "PROVISIONAL — NOT APPROVED — NOT CALCULATED",
    }, ensure_ascii=False).encode("utf-8") + b"\n"
    state_path.write_bytes(raw)
    workbook = tmp_path / "provisional.xlsx"
    workbook.write_bytes(b"not opened")

    result = cli.main(["inspect", "--run-dir", str(run_dir), "--workbook", str(workbook)])

    assert result == cli.BLOCKED
    assert "provisional" in capsys.readouterr().err.lower()
    assert state_path.read_bytes() == raw
    assert not (run_dir / "inspect-report.json").exists()
    assert not (run_dir / "summary.json").exists()


def test_inspect_rejects_forged_approved_state_when_provisional_artifacts_exist(tmp_path: Path, capsys):
    run_dir = tmp_path / "forged-run"
    run_dir.mkdir()
    state_path = run_dir / cli.RUN_STATE
    raw = json.dumps({
        "schema": "airline-valuation-run-state/1",
        "outcome": "awaiting Excel recalculation",
        "saved_sha256": None,
    }).encode("utf-8") + b"\n"
    state_path.write_bytes(raw)
    (run_dir / "provisional.xlsx").write_bytes(b"forged workbook")

    result = cli.main(["inspect", "--run-dir", str(run_dir), "--workbook", str(run_dir / "provisional.xlsx")])

    assert result == cli.BLOCKED
    assert "provisional" in capsys.readouterr().err.lower()
    assert state_path.read_bytes() == raw
    assert not (run_dir / "inspect-report.json").exists()


def test_inspect_rejects_provisional_workbook_name_without_run_state(tmp_path: Path, capsys):
    run_dir = tmp_path / "empty-run"
    run_dir.mkdir()
    workbook = tmp_path / "provisional.xlsx"
    workbook.write_bytes(b"not opened")

    result = cli.main(["inspect", "--run-dir", str(run_dir), "--workbook", str(workbook)])

    assert result == cli.BLOCKED
    assert "provisional" in capsys.readouterr().err.lower()
    assert not (run_dir / cli.RUN_STATE).exists()
    assert not (run_dir / "inspect-report.json").exists()


def test_ordinary_approved_workbook_d62_formula_is_not_classified_as_provisional():
    assert not cli._has_provisional_d62_formula(ROOT / "fcffsimpleginzu.xlsx")


def test_inspect_rejects_guarded_d62_formula_after_rename_without_mutation(tmp_path: Path, capsys):
    run_dir = tmp_path / "forged-run"
    run_dir.mkdir()
    state_path = run_dir / cli.RUN_STATE
    state = {"schema": "airline-valuation-run-state/1", "outcome": "awaiting Excel recalculation", "saved_sha256": None}
    state_path.write_text(json.dumps(state) + "\n", encoding="utf-8")
    workbook = tmp_path / "ordinary-name.xlsx"
    _guarded_formula_copy(ROOT / "fcffsimpleginzu.xlsx", workbook)
    original_workbook_hash = hashlib.sha256(workbook.read_bytes()).hexdigest()
    original_state = state_path.read_bytes()

    result = cli.main(["inspect", "--run-dir", str(run_dir), "--workbook", str(workbook)])

    assert result == cli.BLOCKED
    assert "provisional" in capsys.readouterr().err.lower()
    assert hashlib.sha256(workbook.read_bytes()).hexdigest() == original_workbook_hash
    assert state_path.read_bytes() == original_state
    assert not (run_dir / "inspect-report.json").exists()
    assert not (run_dir / "summary.json").exists()


def test_inspect_fails_closed_on_malformed_package_with_guard_markers(tmp_path: Path, capsys):
    run_dir = tmp_path / "malformed-run"
    run_dir.mkdir()
    workbook = tmp_path / "ordinary-name.xlsx"
    workbook.write_bytes(b"broken package D62 IF(B48=0,0,B50/B49)")

    result = cli.main(["inspect", "--run-dir", str(run_dir), "--workbook", str(workbook)])

    assert result == cli.BLOCKED
    assert "provisional" in capsys.readouterr().err.lower()
    assert not (run_dir / cli.RUN_STATE).exists()
    assert not (run_dir / "inspect-report.json").exists()


def test_validate_rejects_provisional_scenario_json(tmp_path: Path, capsys):
    scenario = ROOT / "examples" / "fy2025-provisional-scenario.json"

    result = cli.main(["validate", "--manifest", str(scenario)])

    assert result == cli.BLOCKED
    assert "NOT READY" in capsys.readouterr().out


def test_prepare_rejects_provisional_scenario_json_without_output(tmp_path: Path, capsys):
    scenario = ROOT / "examples" / "fy2025-provisional-scenario.json"
    output = tmp_path / "prepared"

    result = cli.main(["prepare", "--manifest", str(scenario), "--output-dir", str(output)])

    assert result == cli.BLOCKED
    assert "PREPARE BLOCKED" in capsys.readouterr().err
    assert not output.exists()


def test_provisional_missing_scenario_fails_without_publication(tmp_path, capsys):
    output = tmp_path / "absent-run"
    result = cli.main(["provisional", "--scenario", str(tmp_path / "missing.json"), "--output-dir", str(output)])
    assert result == cli.BLOCKED
    assert "PROVISIONAL BLOCKED" in capsys.readouterr().err
    assert not output.exists()
