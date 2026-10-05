"""Shared immutable inputs for the pinned filing-layer tests and the
public-export private-input gates.

The gating section applies the per-test private-input gates from
``tests/_private_inputs.py`` at collection time.  Tests that need private
inputs (source workbook, example manifests/scenarios, policy-draft JSON, the
draft-scenario fixture, or private review docs) are skipped with an explicit
reason on a clean clone.  When ``AIRLINE_VALUATION_REQUIRE_PRIVATE_INPUTS=1``
they fail instead, so strict private-integration runs never silently skip
coverage.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
from pathlib import Path

import pytest

_TESTS_DIR = Path(__file__).resolve().parent
if str(_TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(_TESTS_DIR))
import _private_inputs as _private

from airline_valuation.filing_ixbrl import parse_filing
from airline_valuation.filing_registry import FILINGS
from airline_valuation.filing_archive import import_filings
from airline_valuation.filing_report import analyze_archive

DEFAULT_FIXTURES = Path.home() / ".local/share/airline-valuation/sec-fixtures-v1"
FIXTURE_DIR_ENV = "AIRLINE_VALUATION_SEC_FIXTURE_DIR"
REQUIRE_FIXTURES_ENV = "AIRLINE_VALUATION_REQUIRE_SEC_FIXTURES"


def sec_fixture_files(*filenames):
    """Return requested private fixtures, skipping or failing when unavailable."""
    configured = os.environ.get(FIXTURE_DIR_ENV)
    fixtures = Path(configured).expanduser() if configured else DEFAULT_FIXTURES
    required = os.environ.get(REQUIRE_FIXTURES_ENV) == "1"
    missing = [name for name in filenames if not (fixtures / name).is_file()]
    if not fixtures.is_dir() or missing:
        detail = f"SEC fixture directory/files unavailable at {fixtures}"
        if missing:
            detail += f"; missing: {', '.join(missing)}"
        if required:
            pytest.fail(detail, pytrace=False)
        pytest.skip(detail)
    return {name: fixtures / name for name in filenames}


@pytest.fixture(scope="session")
def filing_originals():
    """Pinned authentic source bytes, verified against the registry."""
    files = sec_fixture_files(*(
        FILINGS[issuer]["artifacts"]["primary"]["filename"]
        for issuer in ("southwest", "delta")
    ))
    originals = {}
    for issuer in ("southwest", "delta"):
        artifact = FILINGS[issuer]["artifacts"]["primary"]
        raw = files[artifact["filename"]].read_bytes()
        assert len(raw) == artifact["length"]
        assert hashlib.sha256(raw).hexdigest() == artifact["sha256"]
        originals[issuer] = raw
    return originals


@pytest.fixture(scope="session")
def parsed_filing_originals(filing_originals):
    """Parsed authentic originals for read-only tests; copy before mutation."""
    return {
        issuer: parse_filing(raw, source_id=issuer)
        for issuer, raw in filing_originals.items()
    }


@pytest.fixture(scope="session")
def analyzed_filing_archive(tmp_path_factory):
    """One verified two-issuer archive and its immutable report outputs."""
    names = [
        FILINGS[issuer]["artifacts"][kind]["filename"]
        for issuer in ("southwest", "delta")
        for kind in ("submissions", "index", "primary")
    ]
    files = sec_fixture_files(*names)
    root = tmp_path_factory.mktemp("shared-filing-report")
    metadata, documents = root / "metadata", root / "documents"
    metadata.mkdir()
    documents.mkdir()
    for issuer in ("southwest", "delta"):
        for kind in ("submissions", "index"):
            filename = FILINGS[issuer]["artifacts"][kind]["filename"]
            shutil.copyfile(files[filename], metadata / filename)
        filename = FILINGS[issuer]["artifacts"]["primary"]["filename"]
        shutil.copyfile(files[filename], documents / filename)
    archive = import_filings(metadata, documents, root / "archive")
    report = analyze_archive(archive, root / "report")
    return archive, report


# ---------------------------------------------------------------------------
# Public-export private-input gates (per-test, never whole-module).
# ---------------------------------------------------------------------------


def pytest_configure(config):
    """Register the strict-mode fail marker used for missing private inputs."""
    config.addinivalue_line(
        "markers",
        "private_input_required(reason): private input absent and "
        "AIRLINE_VALUATION_REQUIRE_PRIVATE_INPUTS=1 requires a hard failure",
    )


def _item_gate(item) -> tuple[str | None, str]:
    """Return (marker, reason) for a collected item, or (None, '') to run it.

    GATE_BY_FILE maps a module basename to {marker: set of test function
    names}, so each test gates on exactly the private inputs it reads.  The
    marker (and therefore the exact missing-input reason) is chosen per test.
    """
    module = getattr(item, "module", None)
    if module is None or module.__file__ is None:
        return None, ""
    base = Path(module.__file__).name
    by_marker = _private.GATE_BY_FILE.get(base)
    if by_marker is None:
        return None, ""
    # Parametrized tests share the base function name; strip the [params] part.
    name = getattr(item, "originalname", None) or item.name
    for marker, tests in by_marker.items():
        if name not in tests:
            continue
        reason = _private.gate_reason(marker)
        if reason is None:
            return None, ""
        return marker, reason
    return None, ""


def pytest_collection_modifyitems(session, config, items):
    """Apply per-test private-input gates at collection time."""
    strict = os.environ.get(_private.REQUIRE_PRIVATE_INPUTS_ENV) == "1"
    for item in items:
        marker, reason = _item_gate(item)
        if marker is None:
            continue
        if strict:
            item.add_marker(
                pytest.mark.private_input_required(reason=reason)
            )
        else:
            item.add_marker(pytest.mark.skipif(True, reason=reason))


def pytest_runtest_setup(item):
    """Strict mode: fail clearly instead of silently skipping when a private
    input required by this specific test is absent."""
    marks = list(item.iter_markers(name="private_input_required"))
    if not marks:
        return
    reason = marks[-1].kwargs.get("reason", "missing private input")
    pytest.fail(reason, pytrace=False)
