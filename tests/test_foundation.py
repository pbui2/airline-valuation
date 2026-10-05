from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[1]


def test_foundation_metadata():
    agents = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
    project = (ROOT / "docs" / "PROJECT.md").read_text(encoding="utf-8")
    module = (ROOT / "docs" / "modules" / "python.md").read_text(encoding="utf-8")

    assert "Project name:** airline-valuation" in agents
    assert "Project type:** financial-valuation-system" in agents
    assert "Source layout:** src" in agents
    assert "Test command:** python3 -m pytest" in agents
    assert "initial foundation-contract status remains incomplete" in agents.lower()
    assert "Selected modules" in agents
    assert "docs/modules/python.md" in agents
    assert "**Name:** airline-valuation" in project
    assert "**Source layout:** src" in project
    assert "not a valuation calculation engine" in module


def test_required_docs_and_links():
    required = [
        "README.md",
        "AGENTS.md",
        "docs/PROJECT.md",
        "docs/ARCHITECTURE.md",
        "docs/RUNBOOK.md",
        "docs/MAINTENANCE_CHECKLIST.md",
        "docs/DECISIONS.md",
        "docs/CHANGES.md",
        "docs/BACKLOG.md",
        "docs/PROGRESS.md",
        "docs/modules/python.md",
        "docs/progress.md",
        "docs/todo.md",
        "docs/requirements.md",
        "docs/plan.md",
        "docs/source-policy.md",
    ]
    for relative in required:
        assert (ROOT / relative).is_file(), relative

    markdown_files = [ROOT / "README.md", *(ROOT / "docs").glob("*.md")]
    link_pattern = re.compile(r"\[[^\]]+\]\(([^)]+)\)")
    for source in markdown_files:
        text = source.read_text(encoding="utf-8")
        for target in link_pattern.findall(text):
            if target.startswith(("http://", "https://", "mailto:", "#")):
                continue
            target_path = target.split("#", 1)[0]
            if not target_path:
                continue
            assert (source.parent / target_path).resolve().is_file(), (
                f"{source.relative_to(ROOT)} links to missing {target}"
            )


def test_progress_todo_schema():
    progress = (ROOT / "docs" / "PROGRESS.md").read_text(encoding="utf-8")
    detailed = (ROOT / "docs" / "progress.md").read_text(encoding="utf-8")
    todo = (ROOT / "docs" / "todo.md").read_text(encoding="utf-8")
    backlog = (ROOT / "docs" / "BACKLOG.md").read_text(encoding="utf-8")

    for text in (progress, todo, backlog):
        assert "Owner" in text
        assert "Dependency" in text
        assert "Status" in text
    assert "2026-09-27" in progress
    assert "Treasury" in detailed
    assert "Feb. 6 share-price observation and full financial fact set remain unverified" in detailed
    assert "share observation remains pending" in detailed
    assert "evidence-reconciliation-2025.md" in detailed
    assert "Analyst forecast inputs and approvals remain pending" in detailed
    assert "403" in detailed
    assert "human Excel acceptance gate has not been performed" in detailed
    assert "Blocked/not performed; no real saved workbook or attestation exists" in todo
