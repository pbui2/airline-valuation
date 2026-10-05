from __future__ import annotations

from pathlib import Path

from airline_valuation import cli

ROOT = Path(__file__).resolve().parents[1]
SCENARIO = ROOT / "examples/fy2025-provisional-scenario.json"


def _snapshot(output: Path) -> dict[str, bytes]:
    assert {p.name for p in output.iterdir()} == {"provisional.xlsx", "NOTICE.txt", "ledger.json", "run-state.json"}
    return {p.name: p.read_bytes() for p in output.iterdir()}


def _assert_unchanged(output: Path, parent: Path, before: dict[str, bytes]) -> None:
    assert {p.name: p.read_bytes() for p in output.iterdir()} == before
    assert not list(parent.glob(f".{output.name}.stage-*"))
    assert not list(parent.glob(f".{output.name}.backup-*"))


def test_pristine_cost_route_audit_failure_prevents_publication(tmp_path, monkeypatch):
    output = tmp_path / "private"
    args = ["provisional", "--scenario", str(SCENARIO), "--output-dir", str(output)]
    assert cli.main(args) == 0
    before = _snapshot(output)
    monkeypatch.setattr(cli, "validate_verified_driver_evidence",
                        lambda _evidence: (_ for _ in ()).throw(ValueError("injected cost-route audit failure")))
    assert cli.main(args + ["--overwrite"]) == cli.BLOCKED
    _assert_unchanged(output, tmp_path, before)


def test_pristine_input_structure_failure_prevents_publication(tmp_path, monkeypatch):
    from airline_valuation import detailed_inventory

    def fail_pristine(path, *, staged_values=None):
        assert staged_values is None
        raise ValueError("injected pristine Input structure failure")

    output = tmp_path / "private"
    args = ["provisional", "--scenario", str(SCENARIO), "--output-dir", str(output)]
    assert cli.main(args) == 0
    before = _snapshot(output)
    monkeypatch.setattr(detailed_inventory, "validate_input_formula_dependents", fail_pristine)
    assert cli.main(args + ["--overwrite"]) == cli.BLOCKED
    _assert_unchanged(output, tmp_path, before)


def test_staged_input_structure_failure_prevents_publication_and_cleans_stage(tmp_path, monkeypatch):
    from airline_valuation import detailed_inventory

    real_check = detailed_inventory.validate_input_formula_dependents
    calls = 0

    def fail_staged(path, **kwargs):
        nonlocal calls
        calls += 1
        if Path(path).name == "provisional.xlsx":
            assert set(kwargs["staged_values"]) == {"Input sheet!B27", "Input sheet!B28", "Input sheet!B32"}
            raise ValueError("injected staged Input structure failure")
        return real_check(path, **kwargs)

    output = tmp_path / "private"
    args = ["provisional", "--scenario", str(SCENARIO), "--output-dir", str(output)]
    assert cli.main(args) == 0
    before = _snapshot(output)
    monkeypatch.setattr(detailed_inventory, "validate_input_formula_dependents", fail_staged)
    assert cli.main(args + ["--overwrite"]) == cli.BLOCKED
    assert calls >= 2
    _assert_unchanged(output, tmp_path, before)
