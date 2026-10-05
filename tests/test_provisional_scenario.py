import copy
import json
from pathlib import Path

import pytest

from airline_valuation.provisional_scenario import load_provisional_scenario

ROOT = Path(__file__).parents[1]
EXAMPLE = ROOT / "examples/fy2025-provisional-scenario.json"


def _write(tmp_path, data):
    data.setdefault("candidate_dispositions", {"test": "test disposition"})
    if "candidate_inventory" not in data:
        data["candidate_inventory"] = json.loads(EXAMPLE.read_text())["candidate_inventory"]
    for ref, item in data["candidate_inventory"].items():
        if ref in data.get("cells", {}) and item["disposition"] != "entered":
            item["disposition"] = "entered"
            item["reason"] = "test scenario enters preferred inputs"
    path = tmp_path / "scenario.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def test_example_loads_normalized_detailed_mapping():
    result = load_provisional_scenario(EXAMPLE)
    cells = result["cells"]
    assert cells["Input sheet!B13"] == "167"
    assert cells["Input sheet!B15"] == "5981"
    assert cells["Input sheet!B17"] == "No"
    assert cells["Input sheet!B18"] == "3231"
    assert cells["Input sheet!B19"] == cells["Input sheet!B20"] == "0"
    assert cells["Cost of capital worksheet!B32"] == "0"
    assert cells["Input sheet!B21"] == "491.31799"
    assert cells["Input sheet!B22"] == "54.26"
    assert cells["Cost of capital worksheet!B11"] == "Detailed"
    assert cells["Input sheet!B8"] == "Air Transport"
    assert cells["Cost of capital worksheet!B35"] == "Baa2/BBB"
    assert cells["Cost of capital worksheet!B21"] == "Single Business(US)"
    assert cells["Cost of capital worksheet!B25"] == "Operating regions"
    assert cells["Cost of capital worksheet!B48"] == "0"
    assert [float(cells[f"Cost of capital worksheet!H{row}"]) for row in range(21, 32)] == [0, 0, 0, 0, 0, 0, 0, 28063, 0, 0, 0]
    assert "Cost of capital worksheet!B49" not in cells
    assert "Cost of capital worksheet!B50" not in cells
    assert "Cost of capital worksheet!B12" not in cells
    assert sum(float(cells[f"Cost of capital worksheet!H{row}"]) for row in range(21, 32)) == 28063
    assert set(result["uncertainties"]) >= {"rating", "regions", "leases", "preferred"}


def test_seven_growth_profitability_assumptions_and_provenance():
    result = load_provisional_scenario(EXAMPLE)
    raw = json.loads(EXAMPLE.read_text(encoding="utf-8"))
    expected = {
        "Input sheet!B26": ("0.045", "fraction", "IATA global airline 2026 revenue growth forecast 4.5%"),
        "Input sheet!B27": ("0.03", "fraction", "near-term pre-tax operating margin 3%"),
        "Input sheet!B28": ("0.03", "fraction", "independent illustrative long-term revenue growth assumption 3%, not derived from Input sheet B26"),
        "Input sheet!B29": ("0.06", "fraction", "target pre-tax margin 6%"),
        "Input sheet!B30": ("5", "years", "five-year illustrative margin convergence"),
        "Input sheet!B31": ("2", "ratio", "2.0x illustrative sales-to-capital ratio"),
        "Input sheet!B32": ("1.25", "ratio", "years 6–10 sales-to-capital efficiency 1.25x"),
    }
    for ref, (value, unit, basis) in expected.items():
        assert result["cells"][ref] == value
        assert result["units"][ref] == unit
        assert basis in result["provenance"][ref]["basis"]
        assert result["provenance"][ref]["status"]
        assert result["provenance"][ref]["uncertainty"]
        assert raw["candidate_inventory"][ref]["disposition"] == "entered"
        assert raw["candidate_inventory"][ref]["reason"]
    assert "IATA" in result["provenance"]["Input sheet!B26"]["basis"]
    assert "2.5%–3.5%" in result["provenance"]["Input sheet!B28"]["uncertainty"]
    assert "1.0x–1.5x" in result["provenance"]["Input sheet!B32"]["uncertainty"]
    assert "years" in result["units"]["Input sheet!B30"]
    assert "ratio" in result["units"]["Input sheet!B32"]
    assert "second-year" not in json.dumps(raw["provenance"]["Input sheet!B28"]).lower()
    assert "debt maturity" not in json.dumps(raw["provenance"]["Input sheet!B32"]).lower()


def test_c26_c32_label_to_input_mapping():
    raw = json.loads(EXAMPLE.read_text(encoding="utf-8"))
    result = load_provisional_scenario(EXAMPLE)
    expected = {
        "C26": ("Input sheet!B26", "Revenue growth"),
        "C27": ("Input sheet!B27", "Near-term pre-tax operating margin"),
        "C28": ("Input sheet!B28", "Long-term revenue growth"),
        "C29": ("Input sheet!B29", "Target pre-tax operating margin"),
        "C30": ("Input sheet!B30", "Margin convergence years"),
        "C31": ("Input sheet!B31", "Sales-to-capital ratio"),
        "C32": ("Input sheet!B32", "Years 6–10 sales-to-capital efficiency"),
    }
    for label, (ref, meaning) in expected.items():
        assert ref.endswith(label[1:])
        assert meaning.lower() in result["provenance"][ref]["basis"].lower() or label in {"C26", "C27", "C29", "C30", "C31"}
        assert raw["units"][ref] == result["units"][ref]
    assert result["cells"]["Input sheet!B32"] == "1.25"
    assert result["units"]["Input sheet!B32"] == "ratio"
    assert result["cells"]["Cost of capital worksheet!B32"] == "0"
    assert result["units"]["Cost of capital worksheet!B32"] == "years"
    assert "maturity" in result["provenance"]["Cost of capital worksheet!B32"]["basis"]
    assert "not an issuer maturity fact" in result["provenance"]["Cost of capital worksheet!B32"]["uncertainty"]
    assert "second-year" not in json.dumps(raw["candidate_inventory"]["Input sheet!B28"]).lower()
    assert "debt maturity" not in json.dumps(raw["candidate_inventory"]["Input sheet!B32"]).lower()


def test_example_uses_exact_whitelisted_sheet_names_even_when_regex_accepts_case(tmp_path):
    data = json.loads(EXAMPLE.read_text())
    ref = "input sheet!B13"
    data["cells"][ref] = data["cells"].pop("Input sheet!B13")
    data["units"][ref] = data["units"].pop("Input sheet!B13")
    data["provenance"][ref] = data["provenance"].pop("Input sheet!B13")
    with pytest.raises(ValueError, match="unknown or unsupported"):
        load_provisional_scenario(_write(tmp_path, data))


def test_rejects_mismatched_industry_and_rating_lookup_labels(tmp_path):
    for ref, wrong in (("Input sheet!B8", "Airlines"), ("Cost of capital worksheet!B35", "BBB")):
        data = json.loads(EXAMPLE.read_text())
        data["cells"][ref] = wrong
        with pytest.raises(ValueError, match="must be"):
            load_provisional_scenario(_write(tmp_path, data))


def test_candidate_dispositions_are_returned_and_required(tmp_path):
    result = load_provisional_scenario(EXAMPLE)
    assert result["candidate_dispositions"]["industry_key"].startswith("Air Transport")
    assert "no direct WACC" in result["candidate_dispositions"]["method_route"]

    data = json.loads(EXAMPLE.read_text())
    del data["candidate_dispositions"]
    path = tmp_path / "missing-dispositions.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ValueError, match="candidate_dispositions"):
        load_provisional_scenario(path)


def test_candidate_inventory_is_required(tmp_path):
    data = json.loads(EXAMPLE.read_text())
    del data["candidate_inventory"]
    path = tmp_path / "missing-inventory.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ValueError, match="candidate inventory"):
        load_provisional_scenario(path)


def test_rejects_formula_backed_input_cell(tmp_path):
    data = json.loads(EXAMPLE.read_text())
    ref = "Input sheet!B46"
    data["cells"][ref] = 1
    data["units"][ref] = "USD millions"
    data["provenance"][ref] = {"status": "test", "basis": "test", "uncertainty": "test"}
    with pytest.raises(ValueError, match="unknown or unsupported"):
        load_provisional_scenario(_write(tmp_path, data))


def test_rejects_unknown_refs_and_missing_required(tmp_path):
    data = json.loads(EXAMPLE.read_text())
    extra = "Cost of capital worksheet!A1"
    data["cells"][extra] = 1
    data["units"][extra] = "x"
    data["provenance"][extra] = {"status": "x", "basis": "x", "uncertainty": "x"}
    with pytest.raises(ValueError, match="unknown or unsupported"):
        load_provisional_scenario(_write(tmp_path, data))
    data = json.loads(EXAMPLE.read_text())
    for group in ("cells", "units", "provenance"):
        del data[group]["Cost of capital worksheet!H21"]
    with pytest.raises(ValueError, match="missing required"):
        load_provisional_scenario(_write(tmp_path, data))


def test_rejects_overlapping_aggregate_region_even_when_sum_matches(tmp_path):
    data = json.loads(EXAMPLE.read_text())
    data["cells"]["Cost of capital worksheet!H28"] = 0
    data["cells"]["Cost of capital worksheet!H30"] = 27000
    with pytest.raises(ValueError, match="H28 is constrained"):
        load_provisional_scenario(_write(tmp_path, data))


def test_rejects_region_allocation_not_equal_to_revenue(tmp_path):
    data = json.loads(EXAMPLE.read_text())
    data["cells"]["Cost of capital worksheet!H21"] += 1
    with pytest.raises(ValueError, match="H21 must be zero"):
        load_provisional_scenario(_write(tmp_path, data))


def test_preferred_zero_requires_blank_values_and_one_requires_valid_values(tmp_path):
    data = json.loads(EXAMPLE.read_text())
    data["cells"]["Cost of capital worksheet!B49"] = 100
    data["units"]["Cost of capital worksheet!B49"] = "preferred price per share"
    data["provenance"]["Cost of capital worksheet!B49"] = {"status": "assumption", "basis": "test", "uncertainty": "test"}
    with pytest.raises(ValueError, match="switch zero"):
        load_provisional_scenario(_write(tmp_path, data))

    for price, dividend, message in ((0, 1, "B49 must be positive"), (100, -1, "B50 nonnegative finite"), (float("inf"), 1, "finite")):
        case = copy.deepcopy(json.loads(EXAMPLE.read_text()))
        case["cells"]["Cost of capital worksheet!B48"] = 1
        for ref, value, unit in (("Cost of capital worksheet!B49", price, "preferred price per share"), ("Cost of capital worksheet!B50", dividend, "preferred dividend per share")):
            case["cells"][ref] = value
            case["units"][ref] = unit
            case["provenance"][ref] = {"status": "assumption", "basis": "test", "uncertainty": "test"}
        with pytest.raises(ValueError, match=message):
            load_provisional_scenario(_write(tmp_path, case))


def test_preferred_one_accepts_nonzero_price_and_nonnegative_dividend(tmp_path):
    data = json.loads(EXAMPLE.read_text())
    data["cells"]["Cost of capital worksheet!B48"] = 1
    for ref, value, unit in (("Cost of capital worksheet!B49", 100, "preferred price per share"), ("Cost of capital worksheet!B50", 5, "preferred dividend per share")):
        data["cells"][ref] = value
        data["units"][ref] = unit
        data["provenance"][ref] = {"status": "assumption", "basis": "test", "uncertainty": "test"}
    result = load_provisional_scenario(_write(tmp_path, data))
    assert result["cells"]["Cost of capital worksheet!B49"] == "100"
    assert result["cells"]["Cost of capital worksheet!B50"] == "5"


def test_preferred_inputs_require_explicit_per_share_units(tmp_path):
    data = json.loads(EXAMPLE.read_text())
    data["cells"]["Cost of capital worksheet!B48"] = 1
    for ref, value, unit in (("Cost of capital worksheet!B49", 100, "USD"), ("Cost of capital worksheet!B50", 5, "USD")):
        data["cells"][ref] = value
        data["units"][ref] = unit
        data["provenance"][ref] = {"status": "assumption", "basis": "test", "uncertainty": "test"}
    with pytest.raises(ValueError, match="units must identify"):
        load_provisional_scenario(_write(tmp_path, data))


def test_rejects_cost_of_capital_input_and_wrong_units(tmp_path):
    data = json.loads(EXAMPLE.read_text())
    ref = "Cost of capital worksheet!B12"
    data["cells"][ref] = 0.079
    data["units"][ref] = "fraction"
    data["provenance"][ref] = {"status": "assumption", "basis": "test", "uncertainty": "test"}
    with pytest.raises(ValueError, match="unknown or unsupported"):
        load_provisional_scenario(_write(tmp_path, data))
    data = json.loads(EXAMPLE.read_text())
    data["units"]["Input sheet!B21"] = "shares"
    with pytest.raises(ValueError, match="unit must be"):
        load_provisional_scenario(_write(tmp_path, data))


def test_operator_lease_cash_and_nonoperating_decisions(tmp_path):
    scenario = load_provisional_scenario(EXAMPLE)
    cells = scenario["cells"]
    assert cells["Input sheet!B15"] == "5981"
    assert cells["Input sheet!B17"] == "No"
    assert cells["Input sheet!B18"] == "3231"
    assert cells["Input sheet!B19"] == cells["Input sheet!B20"] == "0"
    assert "78 USD millions finance lease debt within reported debt, not added again" in scenario["provenance"]["Input sheet!B15"]["basis"]
    assert "1,080 USD millions recognized operating lease liabilities" in scenario["provenance"]["Input sheet!B15"]["basis"]
    assert "457 million USD of equity securities" in scenario["provenance"]["Input sheet!B19"]["basis"]
    assert "primarily linked to deferred compensation" in scenario["provenance"]["Input sheet!B19"]["basis"]
    assert "not proof that no interest exists" in scenario["provenance"]["Input sheet!B20"]["basis"]
    bad = json.loads(EXAMPLE.read_text())
    bad["cells"]["Input sheet!B17"] = "Yes"
    with pytest.raises(ValueError, match="B17 is constrained"):
        load_provisional_scenario(_write(tmp_path, bad))


def test_zero_maturity_rating_and_selected_route(tmp_path):
    scenario = load_provisional_scenario(EXAMPLE)
    cells = scenario["cells"]
    assert cells["Cost of capital worksheet!B32"] == "0"
    assert cells["Input sheet!B17"] == "No"
    assert cells["Cost of capital worksheet!B33"] == "Actual rating"
    assert cells["Cost of capital worksheet!B35"] == "Baa2/BBB"
    assert "workbook comment fallback" in scenario["provenance"]["Cost of capital worksheet!B32"]["status"]
    assert "Note 6 does not report a single average debt maturity" in scenario["provenance"]["Cost of capital worksheet!B32"]["basis"]
    assert "2025-12-31" in scenario["provenance"]["Cost of capital worksheet!B35"]["basis"]
    assert "PDF pp. 78-79" in scenario["provenance"]["Cost of capital worksheet!B35"]["basis"]
    assert "PDF pp. 87, 109, 113-114, 78-79, 100-101" not in scenario["provenance"]["Cost of capital worksheet!B35"]["basis"]
    data = json.loads(EXAMPLE.read_text())
    data["cells"]["Cost of capital worksheet!B33"] = "Synthetic rating"
    with pytest.raises(ValueError, match="must be"):
        load_provisional_scenario(_write(tmp_path, data))


def test_maturity_domain_and_other_positive_drivers(tmp_path):
    data = json.loads(EXAMPLE.read_text())
    data["cells"]["Cost of capital worksheet!B32"] = -1
    with pytest.raises(ValueError, match="nonnegative"):
        load_provisional_scenario(_write(tmp_path, data))
    data = json.loads(EXAMPLE.read_text())
    data["cells"]["Input sheet!B30"] = 0
    with pytest.raises(ValueError, match="convergence years"):
        load_provisional_scenario(_write(tmp_path, data))
    data = json.loads(EXAMPLE.read_text())
    data["cells"]["Cost of capital worksheet!B32"] = 1
    result = load_provisional_scenario(_write(tmp_path, data))
    assert result["cells"]["Cost of capital worksheet!B32"] == "1"
    data = json.loads(EXAMPLE.read_text())
    data["cells"]["Input sheet!B17"] = "Yes"
    with pytest.raises(ValueError, match="B17 is constrained"):
        load_provisional_scenario(_write(tmp_path, data))
    data = json.loads(EXAMPLE.read_text())
    data["cells"]["Cost of capital worksheet!B32"] = float("inf")
    with pytest.raises(ValueError, match="finite"):
        load_provisional_scenario(_write(tmp_path, data))


def test_north_america_only_allocation_and_provenance():
    result = load_provisional_scenario(EXAMPLE)
    cells = result["cells"]
    assert [float(cells[f"Cost of capital worksheet!H{r}"]) for r in range(21, 32)] == [0, 0, 0, 0, 0, 0, 0, 28063, 0, 0, 0]
    assert "not issuer-reported regional revenue" in result["provenance"]["Cost of capital worksheet!H28"]["uncertainty"]
    assert "one operating segment and provides no geographic allocation" in result["uncertainties"]["regions"]


def test_rejects_published_debt_cash_and_nonoperating_mutations(tmp_path):
    cases = (
        ("Input sheet!B15", 5980),
        ("Input sheet!B18", 3230),
        ("Input sheet!B19", 1),
        ("Input sheet!B20", 1),
    )
    for ref, changed in cases:
        data = json.loads(EXAMPLE.read_text())
        data["cells"][ref] = changed
        with pytest.raises(ValueError, match=f"{ref} is constrained"):
            load_provisional_scenario(_write(tmp_path, data))


def test_rejects_regional_split_preserving_revenue_total(tmp_path):
    data = json.loads(EXAMPLE.read_text())
    data["cells"]["Cost of capital worksheet!H24"] = 1
    data["cells"]["Cost of capital worksheet!H28"] = 28062
    with pytest.raises(ValueError, match="H24 is constrained"):
        load_provisional_scenario(_write(tmp_path, data))


def test_rejects_stale_provenance_for_constrained_values(tmp_path):
    mutations = (
        ("Input sheet!B15", {"basis": "4,901 debt only", "uncertainty": "No lease details"}),
        ("Input sheet!B18", {"basis": "cash is zero", "uncertainty": "investments unknown"}),
        ("Input sheet!B19", {"basis": "no non-operating assets", "uncertainty": "none"}),
        ("Input sheet!B20", {"basis": "no minority interests", "uncertainty": "none"}),
        ("Cost of capital worksheet!H28", {"basis": "all revenue assigned to Caribbean", "uncertainty": "issuer-reported region"}),
    )
    for ref, fields in mutations:
        data = json.loads(EXAMPLE.read_text())
        data["provenance"][ref].update(fields)
        with pytest.raises(ValueError, match=f"{ref} provenance"):
            load_provisional_scenario(_write(tmp_path, data))
