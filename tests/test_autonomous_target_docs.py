import hashlib
import json
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
HISTORICAL_DECISION_ROWS_SHA256 = "cdbc5fc0a8fd81fb29959e758f64a7cd716ee7c595a3715335f7fcfe74612cfd"
WORKBOOK_SHA256 = "c6fd6bd3205e8ef4ecd2b9a01cf81077eaf7db2651d3d02ac03939cb64adabc9"
NEW_DECISION_START = "| 2026-10-01 | Approve a documentation-only future target"


def _doc(name):
    return (ROOT / "docs" / name).read_text(encoding="utf-8")


def _decision_data_rows(text):
    lines = text.splitlines()
    header = next(i for i, line in enumerate(lines) if line.startswith("| Date | Decision |"))
    rows = []
    for line in lines[header + 1 :]:
        if not line.startswith("| "):
            if rows:
                break
            continue
        if re.fullmatch(r"\|\s*[-| ]+\|", line):
            continue
        rows.append(line)
    return rows


def test_autonomous_requirement_and_report_contract():
    for name in ("PROJECT.md", "requirements.md"):
        document = _doc(name)
        if name == "PROJECT.md":
            target = document.split("## Future direction — documentation target only", 1)[1].split("## Roles", 1)[0]
        else:
            target = document.split("## Future autonomous Ginzu input-decision target — not implemented", 1)[1].split("## Project and verification", 1)[0]
        for phrase in (
            "for each company and fiscal period",
            "every eligible Ginzu input value and categorical choice",
            "without routine per-cell human approval" if name == "requirements.md" else "no routine per-cell human approval or signoff",
            "machine-readable and human-readable",
            "source-linked",
            "all 67 executable per-reference specifications" if name == "requirements.md" else "All 67 executable per-reference specifications",
            "composition and no-double-count rules",
            "specified reproducible tie-break",
            "reliable historical input that cannot be sourced",
            "disclosed deterministic per-cell modeled historical fallback is mandatory",
            "approved, versioned rule/formula/parameters",
            "modeled_assumption",
            "never `reported_fact`",
            "fallback trigger, uncertainty, and sensitivity",
            "Each decision report must trace" if name == "requirements.md" else "complete report",
            "rejected alternatives",
            "exact source identity",
            "transformations and arithmetic",
            "currency and scale",
            "diagnostics",
            "no current writer",
            "pending and unresolved",
            "Excel remains the sole valuation computation engine" if name == "requirements.md" else "Excel remains the only valuation computation engine",
        ):
            assert phrase in target, (name, phrase)
        assert "no live source call, source pin, workbook write, or implementation authorization" in target or "no live-call, source-pin, or workbook authorization" in target
        assert "Forecast" in target or "forecast" in target
        assert "selector" in target


def test_autonomous_policy_vocabulary_and_trace():
    documents = {
        "ARCHITECTURE.md": _doc("ARCHITECTURE.md"),
        "source-policy.md": _doc("source-policy.md"),
    }
    headings = (
        "### (a) Source hierarchy",
        "### (b) Cell-specific composition",
        "### (c) Deterministic tie-breaks and modeled fallbacks",
        "### (d) Decision trace",
    )
    required_fields = (
        "Ginzu reference",
        "company, and fiscal period",
        "source statement",
        "reporting period",
        "context",
        "approved",
        "eligible",
        "inclusion",
        "exclusion",
        "double counting",
        "sign",
        "scale",
        "currency",
        "duplicate",
        "competing eligible candidates",
        "reproducible tie-break",
        "missing",
        "versioned",
        "per-reference",
        "modeled_assumption",
        "formula",
        "parameters",
        "never",
        "forecast",
        "selector",
        "chosen value and type",
        "rejected alternatives",
        "exact source identity",
        "locators",
        "arithmetic",
        "policy/specification version",
        "fallback trigger",
        "uncertainty and sensitivity",
        "currency/scale conversion",
        "diagnostics",
        "B15=5,981/B17=No",
        "provisional",
        "Southwest debt remains unresolved",
    )
    semantic_phrases = {
        "ARCHITECTURE.md": (
            (
                "in that order as specified by the reference",
                "any approved note or alternate-source precedence and its exact scope",
                "Do not infer a missing precedence rule",
            ),
            (
                "component signs, currency, source and target scale, and conversion order",
                "prohibit double counting when one reported amount already includes another component",
                "Keep separately reported lease classes distinct",
            ),
            (
                "including identical-priority candidates",
                "a mandatory, disclosed, versioned, per-reference `modeled_assumption` fallback",
                "absent an approved fallback formula/parameters, the reference remains unresolved",
                "separate deterministic, versioned forecast policy",
                "distinct deterministic, versioned policy",
                "classify the result as `selector`, not a reported fact",
            ),
            (
                "all rejected alternatives and rejection reasons",
                "exact source identity and locators",
                "arithmetic and component dependencies",
                "every currency/scale conversion and sign change",
                "fallback trigger/formula/parameters when used",
                "Record uncertainty and sensitivity",
            ),
        ),
        "source-policy.md": (
            (
                "in the order stated by that reference's specification",
                "precedence and exact scope of any approved note or alternate source",
                "leave the rule unresolved rather than infer it",
            ),
            (
                "arithmetic, signs, currency, source scale, target scale, and conversion order",
                "prohibit double counting an amount already included in another component",
                "Keep operating and finance lease amounts distinct",
            ),
            (
                "including equal-priority candidates",
                "require a mandatory, disclosed, deterministic, versioned, per-reference `modeled_assumption` fallback",
                "without approved formula/parameters, leave the reference unresolved",
                "their own deterministic, versioned forecast policy",
                "a distinct deterministic, versioned policy defining the allowed domain",
                "classify their output as `selector`, never a reported fact",
            ),
            (
                "rejected alternatives and reasons",
                "exact source identity and locators",
                "component arithmetic and dependencies",
                "every currency/scale conversion and sign change",
                "fallback trigger, formula, and parameters when used",
                "Record uncertainty and sensitivity",
            ),
        ),
    }
    for name, document in documents.items():
        for heading in headings:
            assert document.count(heading) == 1, (name, heading)
        sections = {}
        for index, heading in enumerate(headings):
            start = document.index(heading) + len(heading)
            end = document.index(headings[index + 1], start) if index + 1 < len(headings) else len(document)
            sections[heading] = document[start:end]
        policy = "\n".join(sections.values())
        for field in required_fields:
            assert field.lower() in policy.lower(), (name, field)
        for heading, phrases in zip(headings, semantic_phrases[name]):
            for phrase in phrases:
                assert phrase in sections[heading], (name, heading, phrase)
        assert "rights" in policy
        assert "first-public availability" in policy
        assert "Forecasts" in policy
        assert "Categorical" in policy
    assert tuple(h for h in headings if h in documents["ARCHITECTURE.md"]) == tuple(h for h in headings if h in documents["source-policy.md"])
    for term in ("source statement", "reporting period", "context", "modeled_assumption", "reported_fact", "calculated_transformation", "forecast", "selector", "B15=5,981/B17=No"):
        assert term in documents["ARCHITECTURE.md"] and term in documents["source-policy.md"], term


def test_autonomous_target_does_not_claim_implementation_or_erase_trial_gates():
    project = _doc("PROJECT.md")
    assert "not implemented" in project
    assert "TEST ONLY" in project
    assert "independent Gold/validator" in project
    assert "source-rights/first-public" in project
    assert "provider-transfer" in project
    assert "nonpromotion" in project
    assert "writer gates" in project
    assert "manual Excel review" in project

    progress = _doc("PROGRESS.md")
    assert "not implemented or authorized for implementation" in progress
    assert "four-airline TEST ONLY" in progress
    assert "independent Gold and validator" in progress
    assert "source-rights/first-public" in progress
    assert "provider-transfer" in progress
    assert "nonpromotion" in progress
    assert "AC.6 writer plus manual Excel review remain open" in progress
    assert "desktop Excel runtime/formula integrity review" in progress

    runbook = _doc("RUNBOOK.md")
    assert "not operational" in runbook
    assert "TEST ONLY four-airline" in runbook
    assert "Gold/validator" in runbook
    assert "rights/first-public" in runbook
    assert "provider-transfer" in runbook
    assert "TEST ONLY nonpromotion" in runbook
    assert "precedes writer work" in runbook
    assert "desktop Excel runtime/formula integrity review" in runbook

    run_guide = _doc("run-guide.md")
    assert "not an available workflow" in run_guide
    assert "TEST ONLY four-airline trial" in run_guide
    assert "independent Gold/validator" in run_guide
    assert "rights/first-public" in run_guide
    assert "provider-transfer" in run_guide
    assert "TEST ONLY outputs never promote" in run_guide
    assert "before writer work" in run_guide
    assert "desktop Excel review" in run_guide

    checklist = _doc("MAINTENANCE_CHECKLIST.md")
    assert "not implemented" in checklist
    assert "four-airline TEST ONLY Gold/validator" in checklist
    assert "rights/first-public" in checklist
    assert "provider-transfer" in checklist
    assert "nonpromotion" in checklist
    assert "before writer work" in checklist
    assert "desktop Excel runtime/formula-integrity review" in checklist

    assert "no live source call, source pin, workbook write, or implementation authorization" in _doc("requirements.md")
    assert "No live calls, source pins, or workbook authorization" in _doc("CHANGES.md")


def test_decision_history_preserved():
    text = _doc("DECISIONS.md")
    rows = _decision_data_rows(text)
    new_rows = [row for row in rows if row.startswith(NEW_DECISION_START)]
    assert len(new_rows) == 1
    historical_rows = [row for row in rows if row != new_rows[0]]
    assert len(historical_rows) == 16
    historical_digest = hashlib.sha256(("\n".join(historical_rows) + "\n").encode("utf-8")).hexdigest()
    assert historical_digest == HISTORICAL_DECISION_ROWS_SHA256
    assert rows[0] == new_rows[0]


def test_documentation_amendment_preserves_pinned_workbook():
    workbook = ROOT / "fcffsimpleginzu.xlsx"
    contract_path = ROOT / "src" / "airline_valuation" / "detailed_contract.json"
    actual_digest = hashlib.sha256(workbook.read_bytes()).hexdigest()
    contract = json.loads(contract_path.read_text(encoding="utf-8"))
    assert actual_digest == WORKBOOK_SHA256
    assert contract["sha256"] == actual_digest
    assert contract["source_file"] == workbook.name == "fcffsimpleginzu.xlsx"
