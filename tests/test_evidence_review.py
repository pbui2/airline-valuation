import hashlib
import subprocess
import sys
from pathlib import Path

from openpyxl import load_workbook

from airline_valuation.evidence_review import PDF_SHA256, build_workbook

ROOT = Path(__file__).parents[1]
TEMPLATE = ROOT / "fcffsimpleginzu.xlsx"
EXPECTED_TEMPLATE_SHA = "c6fd6bd3205e8ef4ecd2b9a01cf81077eaf7db2651d3d02ac03939cb64adabc9"


def test_workbook_content_and_static_review_semantics():
    wb = build_workbook()
    assert wb.sheetnames == ["Read me-status", "Verified 10-K", "Input mapping"]
    readme = wb["Read me-status"]
    joined = " ".join(str(c.value) for row in readme.iter_rows() for c in row if c.value)
    assert "DRAFT — NOT CALCULATED — NOT APPROVED" in joined
    assert "not a valuation model" in joined
    assert PDF_SHA256 in joined
    assert "Publication/availability timestamp is unknown" in joined
    verified = wb["Verified 10-K"]
    records = {verified.cell(r, 1).value: [verified.cell(r, c).value for c in range(1, 8)] for r in range(2, verified.max_row + 1)}
    assert records["Shares outstanding"][1] == "491,317,990"
    assert records["Shares outstanding"][4] == "PDF p. 1"
    assert records["Stockholders’ equity"][4:6] == ["PDF p. 87", "Printed p. 84"]
    assert records["Operating revenues"][4:6] == ["PDF p. 88", "Printed p. 85"]
    mapping = wb["Input mapping"]
    cells = {mapping.cell(r, 1).value: [mapping.cell(r, c).value for c in range(1, 8)] for r in range(2, mapping.max_row + 1)}
    required = {"Input sheet!B11", "Input sheet!B12", "Input sheet!B14", "Input sheet!B15", "Input sheet!B21", "Input sheet!B22", "Input sheet!B23", "Input sheet!B24", "Input sheet!B26", "Input sheet!B29", "Input sheet!B30", "Input sheet!B31", "Input sheet!B34", "Cost of capital worksheet!B11", "Cost of capital worksheet!B12"}
    assert required == set(cells)
    assert "491317990 exact shares" in cells["Input sheet!B21"][2]
    assert "491.31799 million shares" in cells["Input sheet!B21"][4]
    assert "NOT APPROVED" in cells["Input sheet!B21"][5]
    assert "324 + 4,577 = 4,901" in cells["Input sheet!B15"][6]
    assert "122 / 563" in cells["Input sheet!B23"][4]
    assert all(ws.freeze_panes == "A2" for ws in wb.worksheets)
    assert wb["Verified 10-K"].auto_filter.ref
    assert wb["Input mapping"].auto_filter.ref
    assert all(cell.data_type != "f" for ws in wb.worksheets for row in ws.iter_rows() for cell in row)
    wb.close()


def test_generator_refuses_overwrite_unless_flag(tmp_path):
    output = tmp_path / "review.xlsx"
    command = [sys.executable, "-m", "airline_valuation.evidence_review", "--output", str(output)]
    env = {"PYTHONPATH": str(ROOT / "src")}
    result = subprocess.run(command, cwd=ROOT, env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    original = output.read_bytes()
    refused = subprocess.run(command, cwd=ROOT, env=env, capture_output=True, text=True)
    assert refused.returncode != 0
    assert "refusing to overwrite" in refused.stderr
    assert output.read_bytes() == original
    replaced = subprocess.run(command + ["--overwrite"], cwd=ROOT, env=env, capture_output=True, text=True)
    assert replaced.returncode == 0, replaced.stderr
    assert output.exists()


def test_generator_does_not_change_model_template():
    before = hashlib.sha256(TEMPLATE.read_bytes()).hexdigest()
    assert before == EXPECTED_TEMPLATE_SHA
    assert hashlib.sha256(TEMPLATE.read_bytes()).hexdigest() == before
