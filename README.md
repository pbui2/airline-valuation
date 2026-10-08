# airline-valuation — public portfolio excerpt

**Status: portfolio demonstration only. This repository does not contain a
completed valuation and does not include the owner's workbook-backed
valuation inputs or outputs.**

This is a curated public excerpt of a guided manual Excel valuation workflow.
It illustrates the implementation and evidence-control practices used for the
project, and it deliberately stops short of the fully functional private
version.

Eventual Goal: A fully automated valuation system: from collecting SEC filings 
from target company, to conducting rigourous due diligence through independent 
research, to generating defensible assumptions that feed into a valuation 
worksheet, to writing a report detailing and defending the choices taken. 
This does not mean to replace human judgment, but to augment it by generating
a first level overview to be contested and reviewed by a human analyst. 
Investors may also use this to quickly screen for potential investments.

Current status: The repository can collect SEC filings and process information
to be filed in the valuation worksheet. However, the scaffold needs manual
confirmation of data validity, and forward looking assumptions are formed
through general industry trends and guidance without complex research.

## What this repository is

* A portable Python codebase that reads from Damodaran's valuation template,
  validates its structure against pinned fingerprints, and writes prepared
  review/workbook copies to generate a valuation.
* A test suite that runs on a clean clone for the pure-logic and synthetic
  parts, and that explicitly skips the workbook/policy-dependent integration
  tests when the private inputs are not present.
* A workflow demonstration of fail-closed structure checks, pinned-hash
  verification, and evidence review controls.

## What this repository is not

* It is **not** the owner's fully functional workbook-backed version. That
  version — including the original template, the private example
  manifests/scenarios, the policy-draft JSON, and the private review/policy
  documents — is maintained by the project owner **outside** this public
  repository and is not distributed here.
* It does **not** contain a completed or approved valuation, a WACC, or a
  valuation opinion. Nothing here should be read as investment or valuation
  advice.
* It does **not** redistribute the Damodaran template or any derived review
  workbook. See [Rights and attribution](#rights-and-attribution).

## Credit and attribution

The valuation methodology and the original spreadsheet template are by
**Aswath Damodaran**:

* Spreadsheet index / download page:
  <https://pages.stern.nyu.edu/~adamodar/New_Home_Page/spreadsh.htm>
* Direct workbook URL (original source):
  <https://pages.stern.nyu.edu/~adamodar/pc/fcffsimpleginzu.xlsx>

Damodaran's site permits downloading and modifying his spreadsheets, but
public *rehosting* permission was not established for this project, so this
repository does **not** include his template and does not redistribute any
derived review workbooks. The template itself is not part of this public
repository.

### Rights and attribution

* This repository contains original implementation code authored for this
  project. The author makes no license grant over any third-party material
  (including Damodaran's spreadsheet or its contents); a license over the
  original code may be added by the author later.
* Short cell identifiers, pinned fingerprints, and a small number of example
  formula fragments appear as contract data for the functional checks. The
  pinned formula hash/count inventories cover every formula cell in the source
  workbook, but the workbook's full contents are not reproduced here.
* Issuer references span public SEC metadata (CIK numbers, accession numbers,
  and SEC.gov URLs) and public SEC facts and examples — reported financial
  figures and page citations drawn from Southwest Airlines' public FY2025
  Form 10-K. These are recorded as illustrative, unverified working data for
  the evidence-control workflow; they are not a complete valuation, and
  nothing here is investment or valuation advice.

## Getting started (portable)

The code targets Python 3.11+. From a clean clone:

```bash
python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -e . pytest
```

Run the portable suite:

```bash
PYTHONPATH=src .venv/bin/python -m pytest -q
```

On a clone without the private inputs you should see the unit/synthetic tests
pass and the private-input-dependent tests report explicit **skips** (with a
reason naming the missing private input), **not** import or missing-template
errors.

### Optional: restoring workbook-backed behavior (owner-managed)

The workbook-backed workflows need the original template, which you download
directly from Damodaran's site (see [Credit and attribution](#credit-and-attribution)).
Before using any workbook-backed path, verify the downloaded file against the
pinned SHA-256 that the code checks (the pinned digest is stored in
`src/airline_valuation/contract.json` under `sha256`):

```bash
sha256sum fcffsimpleginzu.xlsx
```

A different upstream version of the template will fail the pinned-hash checks
by design; do not bypass them. The private example manifests, scenarios,
policy-draft JSON, and review documents cannot be regenerated from this public
copy, so the full private integration workflows (and their tests) remain
available only where the owner provides those inputs.

## Private vs. full-functional distinction

| Capability | In this public repo | Fully functional private version |
| --- | --- | --- |
| Portable pure-logic + synthetic tests | Yes | Yes |
| Workbook parser/writer invariants (synthetic fixtures) | Yes | Yes |
| Pinned-template workbook integration tests | Explicitly skipped unless you supply the template + private inputs | Yes |
| Private example manifests/scenarios, policy drafts, review docs | No (owner retains) | Yes |
| A completed valuation | No | Owner-only |

## Test layout

* `tests/test_public_synthetic_workbook.py` — synthetic workbook parser/writer
  invariants that run without any private input.
* `tests/test_public_export_contract.py` — portable checks of this README's
  contract (AC.3) and strict private-input mode (AC.5).
* `tests/_private_inputs.py` — the per-test private-input gate map; gates are
  applied per test function, never as whole-module skips.
* `tools/verify_public_export.py` — a standalone export-verification script
  (not collected by pytest) that checks the export acceptance criteria AC.1–6
  against the private source baselines and this repository's tree/history.

## Documentation

* [Project overview](docs/PROJECT.md)
* [Architecture](docs/ARCHITECTURE.md)
* [Source policy / rights](docs/source-policy.md)
* [Run guide](docs/run-guide.md)

## Public-copy notice

This public copy was created from the owner's private repository as a separate
clean history. It intentionally excludes: the original workbook, review
artifacts, policy drafts, private examples/fixtures, and the private review
documents. Earlier private decisions and change history are not silently
restated here; the owner retains the authoritative private record.
