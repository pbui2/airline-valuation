# Project overview

**Name:** airline-valuation

**Type:** portfolio demonstration of a guided manual Excel valuation workflow
support system. This public excerpt is not a valuation engine and does not
contain a completed valuation.

## What the code does

* Reads an optionally provided XLSX template as a read-only ZIP package
  (never recalculating or rewriting a spreadsheet engine result).
* Verifies the package structure, pinned SHA-256, fingerprints, formulas, and
  allowlisted inputs fail-closed against a stored contract.
* Writes prepared/review copies that preserve all unrequested bytes.
* Provides a CLI and supporting review/evidence modules.

## What it deliberately does not do

* It does not run the owner's fully functional workbook-backed workflow from a
  bare clone. That version, and its private inputs (template, manifests,
  scenarios, policy drafts, review documents), remain with the project owner.
* It does not produce a valuation, WACC, or investment opinion.

## Layout

* `src/airline_valuation/` — Python modules and pinned JSON contracts.
* `tests/` — pytest suite (portable unit/synthetic tests plus explicitly gated
  private-input integration tests).
* `tools/verify_public_export.py` — standalone export-verification script
  (AC.1–6), not collected by pytest.

See [Architecture](ARCHITECTURE.md) and [Source policy](source-policy.md).
