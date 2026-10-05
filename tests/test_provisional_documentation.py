from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_reviewable_decision_record_and_automation_backlog():
    decisions = (ROOT / "docs/DECISIONS.md").read_text()
    backlog = (ROOT / "docs/BACKLOG.md").read_text()
    scenario = (ROOT / "docs/provisional-input-scenario.md").read_text()
    evidence = (ROOT / "docs/evidence-reconciliation-2025.md").read_text()
    comment_review = (ROOT / "docs/provisional-comment-review.md").read_text()
    changes = (ROOT / "docs/CHANGES.md").read_text()
    progress = (ROOT / "docs/PROGRESS.md").read_text()
    runbook = (ROOT / "docs/RUNBOOK.md").read_text()
    run_guide = (ROOT / "docs/run-guide.md").read_text()

    # Current guidance tracks the current example and distinguishes only the older saved run as historical.
    for text in (
        "2026-09-28 | Record a distinct private FY2025 Southwest provisional-input decision set",
        "B15=5,981",
        "B18=3,231",
        "B19=0",
        "B20=0",
        "B32=0 years",
        "Baa2/BBB",
        "H24=0",
        "H28=28,063",
        "dd8569563afc978e1b61a41396ba07246ac7a85a4545bfda40c58380d4018306",
        "Revisit B15/B17",
        "not an approved manifest, source-rights clearance, published run, workbook validation, WACC, or valuation.",
        "4,901 debt (324 current maturities + 4,577 long-term debt) already includes 78 finance-lease obligations",
        "Note 6 (issuer Form 10-K PDF p. 109; printed p. 106)",
        "1,080 operating lease liabilities recognized on balance sheet",
        "Note 7 (PDF pp. 113–114; printed pp. 110–111)",
        "Do not add the 78 finance leases twice",
    ):
        assert text in decisions

    assert "source-aware automated resolution research for lease double counting, non-operating asset/minority-interest classifications, debt-maturity availability, and geography disclosure" in backlog
    assert "source provenance and confidence checks" in backlog
    assert "explicit ambiguity escalation" in backlog
    assert "tests for source/page mapping, units, conflicts, and unresolved cases" in backlog
    assert "never silently replace human accounting/model judgment" in backlog
    assert "examples/fy2025-provisional-scenario.json" in scenario
    assert "This current scenario is represented by" in scenario
    assert "## Historical scenario mapping" not in scenario
    assert "| Input sheet!B15 | 5,981 | USD millions |" in scenario
    assert "| Input sheet!B18 | 3,231 | USD millions |" in scenario
    assert "| Cost of capital worksheet!B32 | 0 | years |" in scenario
    assert "H24=0; H28=28,063; all others 0" in scenario
    assert "North America 27,000" not in scenario
    assert "Caribbean 1,063" not in scenario
    assert "B35=5,981" not in scenario
    assert "`error076040_02.xml` reports removal of a formula record from `xl/calcChain.xml`" in comment_review
    assert "repair log is unavailable" not in comment_review
    assert "current `examples/fy2025-provisional-scenario.json` choices" in changes
    assert "example scenario are retained as historical" not in changes

    publication_facts = (
        "private-review/fy2025-lease-cash-north-america-provisional",
        "`provisional.xlsx`, `NOTICE.txt`, `ledger.json`, and `run-state.json`",
        "PROVISIONAL — NOT APPROVED — NOT CALCULATED",
        "8d99e5640a66ad64205a94d368e7dd8aef95d6adc516ca1cdce0ab1ee85ef3ad",
        "c224b6805a5cdeb9c0afbf9f3bbb075b33d7b1887691a208c3ddf06cce1031e7",
        "c6fd6bd3205e8ef4ecd2b9a01cf81077eaf7db2651d3d02ac03939cb64adabc9",
    )
    for text in publication_facts:
        assert text in changes
        assert text in progress
        assert text in run_guide
    assert "all nine older artifact hashes unchanged" in changes
    assert "Parent verified bindings/exact delta" in changes
    assert "full suite passed 181 tests" in changes

    assert "current private-use provisional run is published" in runbook
    assert "parent publication pending" not in run_guide
    assert "This path does not itself encode the new decision values today" not in run_guide
    assert "has not been repaired or recalculated in Excel" in run_guide
    assert "no WACC or valuation is claimed" in run_guide
