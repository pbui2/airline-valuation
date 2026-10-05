# Source policy and rights

## Credit

The valuation methodology and the original spreadsheet template are by
**Aswath Damodaran**:

* Spreadsheet index / download page:
  <https://pages.stern.nyu.edu/~adamodar/New_Home_Page/spreadsh.htm>
* Direct workbook URL (original source):
  <https://pages.stern.nyu.edu/~adamodar/pc/fcffsimpleginzu.xlsx>

Damodaran's site permits downloading and modifying his spreadsheets, but
public rehosting permission was **not** established for this project. This
repository therefore:

* does **not** include the Damodaran template;
* does **not** redistribute any derived review workbooks;
* reproduces only short functional cell identifiers, formulas, and pinned
  fingerprints needed for the checks to operate.

## Rights

* The code in this repository is original implementation authored for this
  project. A license over that original code may be added by the author later.
* No license grant is made or implied over third-party material, including
  Damodaran's spreadsheet and its contents.
* Issuer financial references are limited to public SEC metadata (CIK,
  accession numbers, SEC.gov URLs).

## Private / public boundary

The owner's fully functional workbook-backed version — including the original
template, private example manifests/scenarios, policy-draft JSON, and private
review/policy documents — is maintained **outside** this public repository and
is not distributed here. This public copy cannot regenerate those private
inputs.

## Template integrity

Before any workbook-backed use, download the template directly from the
original source and verify the pinned SHA-256 that the code checks (stored in
`src/airline_valuation/contract.json` under `sha256`). A different upstream
version will fail the pin by design; do not bypass it.
