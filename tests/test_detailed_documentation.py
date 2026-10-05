from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_detailed_workflow_documentation_and_status():
    project = (ROOT / "docs/PROJECT.md").read_text(encoding="utf-8")
    architecture = (ROOT / "docs/ARCHITECTURE.md").read_text(encoding="utf-8")
    scenario = (ROOT / "docs/provisional-input-scenario.md").read_text(encoding="utf-8")
    guide = (ROOT / "docs/run-guide.md").read_text(encoding="utf-8")
    policy = (ROOT / "docs/source-policy.md").read_text(encoding="utf-8")
    decisions = (ROOT / "docs/DECISIONS.md").read_text(encoding="utf-8")
    progress = (ROOT / "docs/PROGRESS.md").read_text(encoding="utf-8")
    backlog = (ROOT / "docs/BACKLOG.md").read_text(encoding="utf-8")
    runbook = (ROOT / "docs/RUNBOOK.md").read_text(encoding="utf-8")
    checklist = (ROOT / "docs/MAINTENANCE_CHECKLIST.md").read_text(encoding="utf-8")
    combined = "\n".join((project, architecture, scenario, guide, policy, decisions,
                          progress, backlog, runbook, checklist))

    assert "detailed-analyze" in guide and "--evidence-bundle" in guide
    assert "--policy-version" in guide and "--output-dir" in guide
    assert "detailed-verify --" not in guide
    assert "report-only" in combined
    assert "no candidate workbook" in combined or "no candidate" in combined
    assert "verified-prepared workbook" in combined
    assert "no Excel valuation" in combined or "Excel valuation" in combined
    assert "B19" in combined and "B20" in combined and "B26:B32" in combined
    assert "page/rights/cutoff/lookup" in combined
    assert "human verification" in combined.lower()
    assert "decision authorship" in combined
    assert "synthetic positive-pipeline" in combined
    assert "cross-company analyst" in combined
    assert "older provisional" in combined and "direct-input" in combined


def test_ac6_superseding_detailed_status_is_documented():
    decisions = (ROOT / "docs/DECISIONS.md").read_text(encoding="utf-8")
    guide = (ROOT / "docs/run-guide.md").read_text(encoding="utf-8")
    project = (ROOT / "docs/PROJECT.md").read_text(encoding="utf-8")
    combined = "\n".join((decisions, guide, project))

    assert "real `detailed-analyze` always publishes an unresolved report package atomically" in decisions
    for artifact in ("decision-report.json", "package.json", "ledger.json", "report.txt", "run-state.json"):
        assert artifact in combined
    assert "prepared_uncalculated" in combined
    assert "Python-only synthetic test fixture" in project
    assert "cannot establish Excel runtime calculation" in project
    assert "cached values remain observations" in project
    assert "does not create or validate human signoff" in guide
    assert "detailed-verify --" not in guide
    assert "recipient reliance" in combined
    assert "source-use rights" in combined
    assert "Historical runs remain distinct" in decisions
    assert "Old direct-input and provisional runs remain separate historical workflows" in decisions
    assert "No Excel calculation, WACC, or valuation" in project
