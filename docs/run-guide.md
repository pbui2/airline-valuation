# Run guide (portable)

This guide covers the portable public suite that runs on a clean clone
without private inputs. It is **not** the owner's full private workflow.

## Prerequisites

* Python 3.11+
* A network connection the first time you install the pinned dependencies.

## Setup

```bash
python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -e . pytest
```

## Run the portable suite

```bash
PYTHONPATH=src .venv/bin/python -m pytest -q
```

Expected result on a clean clone: unit/synthetic tests pass; private-input
integration tests are **skipped** with an explicit reason naming the missing
private input. There should be no import or missing-template errors.

## Strict private-input mode

To verify that the private-input gates fail (rather than silently skip) when
their inputs are absent, run with the strict flag:

```bash
AIRLINE_VALUATION_REQUIRE_PRIVATE_INPUTS=1 PYTHONPATH=src .venv/bin/python -m pytest -q tests/test_workbook.py
```

You should see errors with a clear "requires private ..." reason.

## Workbook-backed paths (owner-managed)

Workbook-backed workflows require the template and the owner's private
inputs, which this public repository does not distribute. Download the
template from the original source and verify its pinned SHA-256 before use;
see [Source policy](source-policy.md).
