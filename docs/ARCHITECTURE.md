# Architecture

## Overview

The system treats an XLSX template as a ZIP package and never depends on a
spreadsheet engine to calculate values. All structure and value checks are
pinned to a stored contract of fingerprints and hashes.

## Modules

* `workbook.py` — read-only template reader. Validates package integrity
  (ZIP members, relationships, content types, sheets, cells), computes
  fingerprints, and exposes fail-closed `verify_identity` / `verify_structure`
  / `read_input` / `read_output` / `branch_status`.
* `ooxml_writer.py`, `provisional_writer.py` — writers that clone a pinned
  template byte-for-byte and replace only explicitly requested direct input
  cells, refusing formula cells, unallowlisted cells, and non-pinned
  templates.
* `manifest.py`, `sec.py`, `filing_*.py` — evidence/review layer over public
  SEC metadata (CIK, accession numbers, SEC.gov URLs).
* `cli.py` — command-line entry point.
* `contract.json`, `provisional_contract.json`, `detailed_contract.json` —
  pinned contracts containing the template SHA-256 and structural
  fingerprints. The template itself is **not** distributed publicly.

## Fail-closed principles

1. A non-pinned template is rejected before any write.
2. A tampered value, formula, relationship, or package member is rejected.
3. Workbook-backed operations that require private inputs are gated per test
   (or fail in strict mode), never silently assumed available.

## Private-input boundary

Anything requiring the original workbook, example manifests/scenarios,
policy-draft JSON, or private review documents is outside this public copy.
See [Source policy](source-policy.md).
