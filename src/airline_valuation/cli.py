"""Guided manual Excel workflow. This module never calculates or saves XLSX data."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import shutil
import sys
import tempfile
import zipfile
import xml.etree.ElementTree as ET
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from .detailed_inventory import inspect_detailed_workbook, validate_verified_driver_evidence
from .detailed_workflow import analyze as detailed_analyze
from .provisional_scenario import load_provisional_scenario
from .provisional_writer import write_provisional, verify_provisional_delta
from .manifest import assess_readiness, normalize_entry, validate_manifest
from .ooxml_writer import verify_prepared_delta, write_prepared
from .workbook import Workbook, WorkbookContractError
from .filing_archive import import_filings
from .filing_report import analyze_archive, analyze_archive_test_fixture
from .filing_registry import FILINGS
from .sec import SECFilingClient

BLOCKED, MISSING = 2, 3
ATTESTATION_SCHEMA = "airline-valuation-excel-attestation/1"
RUN_STATE = "run-state.json"
ATTESTATION = "attestation.json"


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def load_json(path: Path) -> tuple[dict[str, Any] | None, bytes | None, list[str]]:
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return None, None, [f"missing file: {path}"]
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        return None, raw, [f"malformed JSON in {path}: {e}"]
    if not isinstance(value, dict):
        return None, raw, [f"{path} must be a JSON object"]
    return value, raw, []


def _refs(item: dict[str, Any]) -> list[str]:
    value = item.get("workbook_cells", item.get("workbook_cell"))
    if isinstance(value, str):
        return [value]
    return value if isinstance(value, list) and all(isinstance(x, str) for x in value) else []


def mappings(manifest: dict[str, Any]) -> list[tuple[str, Any, dict[str, Any], str]]:
    result = []
    for collection in ("facts", "market_observations", "analyst_assumptions"):
        for item in manifest.get(collection, []):
            if isinstance(item, dict):
                result.extend((ref, item.get("value"), item, collection) for ref in _refs(item))
    return result


def _manifest_state(manifest: dict[str, Any]) -> tuple[list[str], list[str]]:
    validation = validate_manifest(manifest)
    blockers = list(assess_readiness(manifest).blockers)
    return list(validation.errors), list(dict.fromkeys(blockers))


def _citation(item: dict[str, Any]) -> dict[str, Any]:
    source = item.get("source")
    if not isinstance(source, dict):
        source = item if any(k in item for k in ("url", "description", "accessed_at")) else {}
    return {k: source.get(k) for k in ("url", "accessed_at", "description", "accession", "sha256") if k in source}


def _review_data(manifest: dict[str, Any], errors: list[str], blockers: list[str]) -> dict[str, Any]:
    evidence = []
    for collection in ("facts", "market_observations"):
        for item in manifest.get(collection, []):
            if isinstance(item, dict):
                evidence.append({"kind": collection, "id": item.get("id"), "value": item.get("value"), "unit": item.get("unit"), "status": item.get("status"), "as_of": item.get("as_of"), "workbook_cells": _refs(item), "citation": _citation(item)})
    decisions = [{"id": d.get("id"), "question": d.get("question", d.get("decision")), "status": d.get("status"), "selected": d.get("value"), "options": d.get("options"), "rationale": d.get("rationale")} for d in manifest.get("analyst_decisions", []) if isinstance(d, dict)]
    warnings = list(manifest.get("warnings", [])) if isinstance(manifest.get("warnings", []), list) else []
    if not mappings(manifest):
        warnings.append("No mapped input cells are currently associated with evidence; this pending review state is not malformed by itself.")
    return {"status": "not ready" if blockers else "ready for manual review", "entity": manifest.get("entity"), "fiscal_period": manifest.get("fiscal_period"), "cutoff": manifest.get("cutoff"), "approval": manifest.get("approval"), "evidence_and_citations": evidence, "key_decisions": decisions, "draft_entry": {"analyst_assumptions": manifest.get("analyst_assumptions", []), "review_packet": manifest.get("review_packet", {})}, "warnings": warnings, "validation_errors": errors, "blockers": blockers}


def cmd_review(a: argparse.Namespace) -> int:
    manifest, _, errs = load_json(a.manifest)
    if errs:
        print("REVIEW ERROR\n" + "\n".join("- " + e for e in errs), file=sys.stderr)
        return MISSING if errs[0].startswith("missing file:") else BLOCKED
    assert manifest is not None
    validation_errors, blockers = _manifest_state(manifest)
    no_mappings = not mappings(manifest)
    pending_evidence = all(item.get("status") in {"pending", "unknown", "not_applicable"} for group in ("facts", "market_observations", "analyst_assumptions") for item in manifest.get(group, []) if isinstance(item, dict))
    if no_mappings and pending_evidence:
        expected_incomplete = [e for e in validation_errors if e == "required_cells must exactly match the reviewed workbook contract"]
        validation_errors = [e for e in validation_errors if e not in expected_incomplete]
        blockers = list(dict.fromkeys(blockers + ["Pending review has no mapped input cells; contract readiness remains blocked."]))
    if validation_errors:
        print("REVIEW INVALID\n" + "\n".join("- " + e for e in validation_errors), file=sys.stderr)
        return BLOCKED
    packet = _review_data(manifest, validation_errors, blockers)
    a.output_dir.mkdir(parents=True, exist_ok=True)
    path = a.output_dir / "review-packet.json"
    if path.exists() and not a.overwrite:
        print(f"refusing to overwrite {path}; pass --overwrite", file=sys.stderr)
        return BLOCKED
    _atomic_json(path, packet, overwrite=a.overwrite)
    print(json.dumps(packet, indent=2, ensure_ascii=False))
    return 0


def _contract() -> dict[str, Any]:
    path = Path(__file__).with_name("contract.json")
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("contract.json must be an object")
    return data


def _mapped_for_prepare(manifest: dict[str, Any], contract: dict[str, Any]) -> tuple[list[tuple[str, Any, dict[str, Any], str]], list[str]]:
    rows = mappings(manifest)
    blockers: list[str] = []
    by_ref: dict[str, tuple[str, Any, dict[str, Any], str]] = {}
    allowlist = set(contract.get("input_allowlist", []))
    metadata = contract.get("input_metadata", {})
    for row in rows:
        ref, value, item, collection = row
        if ref in by_ref:
            blockers.append(f"duplicate mapped cell: {ref}")
        by_ref[ref] = row
        if ref not in allowlist:
            blockers.append(f"mapped cell is not allowlisted: {ref}")
            continue
        meta = metadata.get(ref)
        if not isinstance(meta, dict):
            blockers.append(f"contract has no input_metadata for {ref}")
            continue
        if value is None or item.get("status") not in {"approved", "verified", "complete", "resolved"}:
            blockers.append(f"mapped input is not an approved value: {ref}")
        try:
            _normalize_mapped_entry(ref, item, meta)
        except (TypeError, ValueError, KeyError) as exc:
            blockers.append(f"unit/scale mismatch or normalization failure for {ref}: {exc}")
        if meta.get("kind") == "numeric":
            try:
                n = Decimal(str(value))
                if not n.is_finite():
                    raise InvalidOperation
            except (InvalidOperation, ValueError):
                blockers.append(f"{ref} requires a finite numeric value")
        elif meta.get("kind") == "switch" and value not in meta.get("allowed", []):
            blockers.append(f"unsupported switch value for {ref}: {value!r}")
    required = set(contract.get("required_inputs", []))
    mode_ref = "Cost of capital worksheet!B11"
    mode = by_ref.get(mode_ref)
    if mode is None or mode[1] != "I will input":
        blockers.append("supported cost-of-capital selector Cost of capital worksheet!B11='I will input' must be explicitly mapped")
    if not required <= set(by_ref):
        blockers.extend(f"required contract input is not mapped: {r}" for r in sorted(required - set(by_ref)))
    manifest_required = set(manifest.get("required_cells", [])) if isinstance(manifest.get("required_cells", []), list) else set()
    if manifest_required and manifest_required != set(by_ref):
        blockers.append("manifest.required_cells does not equal the actual mapped input cells")
    return rows, list(dict.fromkeys(blockers))


def _normalize_mapped_entry(ref: str, item: dict[str, Any], metadata: dict[str, Any]) -> tuple[str, str]:
    if metadata.get("kind") == "switch":
        value = item.get("value")
        if value not in metadata.get("allowed", []):
            raise ValueError(f"{ref} is not an approved selector choice")
        return str(value), str(metadata.get("unit", "selector choice"))
    return normalize_entry(ref, item, metadata)


def _atomic_json(path: Path, value: Any, *, overwrite: bool) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(value, f, indent=2, ensure_ascii=False)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        if path.exists() and not overwrite:
            raise FileExistsError(path)
        os.replace(temp_name, path)
    finally:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass


def _is_provisional_scenario(value: dict[str, Any]) -> bool:
    """Recognize the unaudited scenario document, not an approved manifest."""
    return all(key in value for key in ("scenario_name", "cells", "units", "provenance"))


def cmd_validate(a: argparse.Namespace) -> int:
    manifest, _, errs = load_json(a.manifest)
    if errs:
        print("INVALID\n" + "\n".join("- " + e for e in errs))
        return MISSING if errs[0].startswith("missing file:") else BLOCKED
    assert manifest is not None
    if _is_provisional_scenario(manifest):
        print("NOT READY\n- provisional scenario JSON is not an approved manifest")
        return BLOCKED
    errors, blockers = _manifest_state(manifest)
    print("READY" if not errors and not blockers else "NOT READY")
    for e in errors + blockers:
        print("- " + e)
    return 0 if not errors and not blockers else BLOCKED


def _reject_symlink_path(path: Path) -> None:
    absolute = Path(os.path.abspath(path))
    for candidate in (absolute, *absolute.parents):
        if candidate.is_symlink():
            raise ValueError(f"refusing symlink path component: {candidate}")


def _validate_staged_run(stage: Path, template: Path, normalized: dict[str, str], state: dict[str, Any], manifest_hash: str) -> None:
    required = {"prepared.xlsx", "checklist.txt", "ledger.jsonl", RUN_STATE}
    actual = {path.name for path in stage.iterdir()}
    if actual != required or any(not (stage / name).is_file() or (stage / name).is_symlink() for name in required):
        raise ValueError("staged run is incomplete or contains unexpected artifacts")
    verify_prepared_delta(template, stage / "prepared.xlsx", normalized)
    if state.get("prepared_sha256") != sha256(stage / "prepared.xlsx"):
        raise ValueError("staged prepared workbook hash differs from run-state")
    records = [json.loads(line) for line in (stage / "ledger.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
    if len(records) != 1 or records[0].get("manifest_sha256") != manifest_hash:
        raise ValueError("staged ledger is incomplete or has the wrong manifest binding")
    entries = {item.get("cell"): item for item in records[0].get("items", []) if isinstance(item, dict)}
    if any(entries.get(ref, {}).get("normalized_value") != value for ref, value in normalized.items()):
        raise ValueError("staged ledger normalized values do not match prepared inputs")
    staged_state, _, errors = load_json(stage / RUN_STATE)
    if errors or staged_state != state:
        raise ValueError("staged run-state is unreadable or inconsistent")


def _validate_existing_run(path: Path) -> None:
    if path.is_symlink() or not path.is_dir():
        raise ValueError(f"refusing unsafe existing run path: {path}")
    for root, dirs, files in os.walk(path, followlinks=False):
        root_path = Path(root)
        if any((root_path / name).is_symlink() for name in dirs + files):
            raise ValueError(f"refusing symlink inside existing run: {root_path}")
        if any(not (root_path / name).is_file() for name in files):
            raise ValueError(f"refusing non-file artifact in existing run: {root_path}")


def cmd_prepare(a: argparse.Namespace) -> int:
    outdir = a.output_dir
    manifest_path = a.manifest
    manifest, raw, errs = load_json(manifest_path)
    if errs:
        print("PREPARE BLOCKED\n" + "\n".join("- " + e for e in errs), file=sys.stderr)
        return MISSING if errs[0].startswith("missing file:") else BLOCKED
    assert manifest is not None and raw is not None
    if _is_provisional_scenario(manifest):
        print("PREPARE BLOCKED: provisional scenario JSON is not an approved manifest", file=sys.stderr)
        return BLOCKED
    validation_errors, blockers = _manifest_state(manifest)
    try:
        contract = _contract()
        template = Path(__file__).resolve().parents[2] / str(contract.get("source_file", "fcffsimpleginzu.xlsx"))
        rows, map_blockers = _mapped_for_prepare(manifest, contract)
        blockers.extend(map_blockers)
        blockers.extend(validation_errors)
        if blockers:
            raise ValueError("manifest/contract not ready: " + "; ".join(dict.fromkeys(blockers)))
        with Workbook(template) as wb:
            if not wb.verify_identity().get("matches_template"):
                raise ValueError("template identity hash differs from approved contract")
            wb.verify_structure()
        normalized: dict[str, str] = {}
        items: list[dict[str, Any]] = []
        lines = ["Approved inputs are populated in the prepared workbook. Open it in Excel, recalculate, review, and save; Python does not calculate valuations."]
        metadata = contract.get("input_metadata", {})
        for i, (ref, value, item, _collection) in enumerate(rows, 1):
            meta = metadata[ref]
            normalized_value, target_unit = _normalize_mapped_entry(ref, item, meta)
            normalized[ref] = str(normalized_value)
            entry = {"index": i, "cell": ref, "id": item.get("id"), "raw_value": value,
                     "raw_unit": item.get("unit"), "normalized_value": str(normalized_value),
                     "normalized_unit": target_unit, "source": _citation(item)}
            items.append(entry)
            lines.append(f"{i}. {ref} — {meta.get('meaning', item.get('id', 'input'))}: {normalized_value} [{target_unit}]")
    except (OSError, KeyError, TypeError, ValueError, WorkbookContractError) as e:
        print(f"PREPARE BLOCKED: {e}", file=sys.stderr)
        return BLOCKED

    parent = outdir.parent
    try:
        _reject_symlink_path(outdir)
        parent.mkdir(parents=True, exist_ok=True)
        orphan_backups = sorted(parent.glob(f".{outdir.name}.backup-*"))
        if orphan_backups:
            paths = ", ".join(str(path) for path in orphan_backups)
            raise ValueError(
                f"orphan run backup(s) detected: {paths}. Do not rerun with --overwrite or delete these backups; "
                f"inspect the backup, then manually restore it to {outdir} (or move it to a separately reviewed recovery location) before preparing again."
            )
        if outdir.exists() and not a.overwrite:
            raise FileExistsError(f"run directory already exists: {outdir}; pass --overwrite")
        if outdir.exists():
            _validate_existing_run(outdir)
            # Retain the approved prepare overwrite behavior for ordinary prior
            # directories, but never consume provisional artifacts or state.
            existing_state, _, _ = load_json(outdir / RUN_STATE)
            if (outdir / "provisional.xlsx").exists() or (outdir / "NOTICE.txt").exists() or (isinstance(existing_state, dict) and str(existing_state.get("schema", "")).startswith("airline-valuation-provisional")):
                raise ValueError("refusing overwrite of a provisional run from approved prepare")
        stage = Path(tempfile.mkdtemp(prefix=f".{outdir.name}.stage-", dir=parent))
        backup = parent / f".{outdir.name}.backup-{os.getpid()}"
        if backup.exists() or backup.is_symlink():
            raise FileExistsError(f"ambiguous backup path exists: {backup}")
        try:
            prepared = stage / "prepared.xlsx"
            write_prepared(template, prepared, normalized)
            prepared_hash = sha256(prepared)
            manifest_hash = hashlib.sha256(raw).hexdigest()
            (stage / "checklist.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
            (stage / "ledger.jsonl").write_text(json.dumps({"event": "prepared", "manifest_sha256": manifest_hash, "items": items}, ensure_ascii=False) + "\n", encoding="utf-8")
            state = {"schema": "airline-valuation-run-state/1", "manifest_path": str(manifest_path.resolve()),
                     "manifest_sha256": manifest_hash, "template_path": str(template.resolve()),
                     "template_sha256": sha256(template), "prepared_path": str((outdir / "prepared.xlsx").resolve()),
                     "prepared_sha256": prepared_hash, "saved_path": None, "saved_sha256": None,
                     "outcome": "awaiting Excel recalculation"}
            (stage / RUN_STATE).write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
            _validate_staged_run(stage, template, normalized, state, manifest_hash)
            moved_old = False
            try:
                if outdir.exists():
                    os.replace(outdir, backup)
                    moved_old = True
                os.replace(stage, outdir)
            except OSError:
                if moved_old and not outdir.exists():
                    try:
                        os.replace(backup, outdir)
                    except OSError as rollback_error:
                        raise OSError(f"publication failed and backup restoration failed; previous run remains at {backup}: {rollback_error}")
                raise
            if moved_old:
                try:
                    shutil.rmtree(backup)
                except OSError as cleanup_error:
                    print(f"WARNING: new run was published successfully; previous run backup remains at {backup}: {cleanup_error}", file=sys.stderr)
        finally:
            if stage.exists():
                shutil.rmtree(stage)
    except (OSError, ValueError) as e:
        print(f"PREPARE FAILED: {e}", file=sys.stderr)
        return BLOCKED
    print(f"Prepared workbook: {outdir / 'prepared.xlsx'}\nRun state: {outdir / RUN_STATE}\nChecklist: {outdir / 'checklist.txt'}")
    return 0


def _attestation_errors(att: dict[str, Any], expected: dict[str, str]) -> list[str]:
    errors = []
    fields = {"schema": ATTESTATION_SCHEMA, **expected}
    for key, value in fields.items():
        if att.get(key) != value:
            errors.append(f"attestation {key} mismatch")
    if not isinstance(att.get("excel_version"), str) or not att["excel_version"].strip(): errors.append("Excel version is missing")
    if att.get("calculation_mode") != "Automatic": errors.append("calculation_mode must be Automatic")
    for key in ("full_recalculation", "saved", "reopened"):
        if att.get(key) is not True: errors.append(f"attestation {key} must be true")
    if not isinstance(att.get("iteration_enabled"), bool): errors.append("iteration_enabled must be boolean")
    elif att["iteration_enabled"]:
        if not isinstance(att.get("iteration_count"), int) or att["iteration_count"] <= 0: errors.append("iteration_count must be positive")
        if not isinstance(att.get("iteration_maximum_change"), (int, float)) or att["iteration_maximum_change"] <= 0: errors.append("iteration_maximum_change must be positive")
    elif att.get("iteration_count") is not None or att.get("iteration_maximum_change") is not None: errors.append("disabled iteration settings must be null")
    if not isinstance(att.get("signed_by"), str) or not att["signed_by"].strip(): errors.append("signed_by is required")
    try: date.fromisoformat(att.get("signed_date", ""))
    except (TypeError, ValueError): errors.append("signed_date must be ISO YYYY-MM-DD")
    if not isinstance(att.get("signature"), str) or not att["signature"].strip(): errors.append("signed signature declaration is required")
    return errors


def _finite_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _same_cell_value(left: Any, right: Any) -> bool:
    """Compare cached XLSX values without coercing booleans or text into numbers."""
    if isinstance(left, bool) or isinstance(right, bool):
        return type(left) is type(right) and left == right
    if isinstance(left, (int, float, Decimal)) and isinstance(right, (int, float, Decimal)):
        try:
            return Decimal(str(left)) == Decimal(str(right))
        except InvalidOperation:
            return False
    return type(left) is type(right) and left == right


def _compare_allowlisted_inputs(contract: dict[str, Any], prepared_inputs: dict[str, Any], saved_reader: Any,
                                manifest: dict[str, Any] | None) -> tuple[dict[str, Any], list[str]]:
    rows = mappings(manifest) if manifest is not None else []
    mapped: dict[str, list[tuple[Any, dict[str, Any]]]] = {}
    issues: list[str] = []
    for ref, _raw_value, item, _ in rows:
        meta = contract.get("input_metadata", {}).get(ref, {})
        try:
            expected, _target_unit = _normalize_mapped_entry(ref, item, meta)
        except (TypeError, ValueError, KeyError) as exc:
            issues.append(f"cannot normalize approved input {ref}: {exc}")
            continue
        if meta.get("kind") == "numeric":
            try:
                expected = Decimal(str(expected))
            except InvalidOperation:
                issues.append(f"normalized input is not numeric: {ref}")
        mapped.setdefault(ref, []).append((expected, item))
    refs: dict[str, Any] = {}
    for ref in contract.get("input_allowlist", []):
        if not isinstance(ref, str) or "!" not in ref or ref not in prepared_inputs:
            continue
        sheet, cell = ref.rsplit("!", 1)
        actual = saved_reader(cell, sheet)
        original = prepared_inputs[ref]
        changed = not _same_cell_value(actual, original)
        entries = mapped.get(ref, [])
        refs[ref] = {"saved_value": actual, "prepared_default": original, "changed_from_prepared": changed,
                     "analyst_approved_mapping": bool(entries)}
        if changed and not entries:
            issues.append(f"unapproved workbook input mutation: {ref}")
        for expected, item in entries:
            if item.get("status") not in {"approved", "verified", "complete", "resolved", "ready"}:
                issues.append(f"changed or mapped input is not analyst-approved: {ref}")
            if not _same_cell_value(actual, expected):
                issues.append(f"saved input mismatch: {ref}")
        if not changed and not entries:
            refs[ref]["interpretation"] = "unchanged template default; not an analyst decision"
    return refs, issues


def _identities(wb: Workbook) -> list[str]:
    # Verify arithmetic identities only against saved cached cells; never compute/report valuations.
    equations = [
        ("B29", ["B24", "B25", "B26", "B27", "B28"], [1, -1, -1, 1, 1], "B29=B24-B25-B26+B27+B28"),
        ("B31", ["B29", "B30"], [1, -1], "B31=B29-B30"),
        ("B33", ["B31", "B32"], [1, 1], "B33=B31/B32"),
    ]
    errors = []
    for lhs, operands, signs, label in equations:
        try:
            vals = [wb.read_output(cell, "Valuation output") for cell in [lhs, *operands]]
        except (PermissionError, KeyError):
            errors.append(f"identity saved-cell refs unavailable: Valuation output!{label}")
            continue
        if not all(_finite_number(v) for v in vals):
            errors.append(f"identity cached cells unavailable/non-finite: Valuation output!{label}")
            continue
        if label == "B33=B31/B32":
            expected = vals[1] / vals[2] if vals[2] != 0 else float("nan")
        else:
            expected = sum(sign * value for sign, value in zip(signs, vals[1:]))
        if not math.isfinite(expected) or not math.isclose(vals[0], expected, rel_tol=1e-9, abs_tol=1e-9):
            errors.append(f"cached consistency identity failed: Valuation output!{label}")
    return errors


def _persist_inspection(run_dir: Path, report: dict[str, Any], state_path: Path | None = None, state: dict[str, Any] | None = None) -> None:
    _atomic_json(run_dir / "inspect-report.json", report, overwrite=True)
    summary = {k: report.get(k) for k in ("status", "source_manifest", "manifest_sha256", "prepared_sha256", "saved_workbook", "saved_sha256", "valuation_cutoff", "attestation_outcome", "source_ledger", "assumptions", "diagnostics", "saved_cell_references", "issues", "limitations")}
    # The public summary reports only values read from the submitted Excel-saved
    # workbook; prepared defaults and comparison results stay in inspect-report.
    saved_cells = summary.get("saved_cell_references")
    if isinstance(saved_cells, dict):
        summary["saved_cell_references"] = {
            ref: {"saved_value": entry.get("saved_value")}
            for ref, entry in saved_cells.items()
            if isinstance(entry, dict) and "saved_value" in entry
        }
    _atomic_json(run_dir / "summary.json", summary, overwrite=True)
    if state_path is not None and state is not None:
        state.update({"saved_path": report.get("saved_workbook"), "saved_sha256": report.get("saved_sha256"), "outcome": report["status"], "issues": report.get("issues", [])})
        _atomic_json(state_path, state, overwrite=True)


_OOXML_MAIN_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_OOXML_REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_OOXML_PACKAGE_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
_PROVISIONAL_D62_FORMULA = "IF(B48=0,0,B50/B49)"


def _has_provisional_d62_formula(workbook_path: Path) -> bool:
    """Read-only detection of the known guarded provisional formula in D62."""
    try:
        with zipfile.ZipFile(workbook_path) as archive:
            workbook_root = ET.fromstring(archive.read("xl/workbook.xml"))
            rels_root = ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
            rels = {node.attrib.get("Id"): node.attrib.get("Target") for node in rels_root.findall(f"{{{_OOXML_PACKAGE_REL_NS}}}Relationship")}
            sheets = workbook_root.findall(f"{{{_OOXML_MAIN_NS}}}sheets/{{{_OOXML_MAIN_NS}}}sheet")
            for sheet in sheets:
                if sheet.attrib.get("name") != "Cost of capital worksheet":
                    continue
                target = rels.get(sheet.attrib.get(f"{{{_OOXML_REL_NS}}}id"))
                if not target:
                    break
                part = target.lstrip("/") if target.startswith("/") else "xl/" + target
                part = str(Path(part))
                root = ET.fromstring(archive.read(part))
                for cell in root.findall(f".//{{{_OOXML_MAIN_NS}}}c"):
                    if cell.attrib.get("r") == "D62":
                        formula = cell.find(f"{{{_OOXML_MAIN_NS}}}f")
                        return formula is not None and (formula.text or "").replace(" ", "").casefold() == _PROVISIONAL_D62_FORMULA.casefold()
                return False
            return False
    except (OSError, KeyError, zipfile.BadZipFile, ET.ParseError, ValueError) as exc:
        # Only elevate malformed packages that contain clear markers of the
        # guarded formula; ordinary unreadable workbooks follow normal inspect.
        try:
            raw = workbook_path.read_bytes()
        except OSError:
            return False
        marker = b"IF(B48=0,0,B50/B49)"
        if marker.lower() in raw.lower() and b"D62" in raw:
            return True
        return False


def _is_provisional_run(run_dir: Path, state: dict[str, Any] | None, workbook_path: Path) -> bool:
    """Identify provisional artifacts before inspect can persist approved-run state."""
    if isinstance(state, dict) and str(state.get("schema", "")).startswith("airline-valuation-provisional"):
        return True
    # Provisional publication uses its own workbook and notice names. Do not
    # let a forged approved schema turn those artifacts into inspectable runs.
    if (run_dir / "provisional.xlsx").exists() or (run_dir / "NOTICE.txt").exists():
        return True
    if workbook_path.name == "provisional.xlsx":
        return True
    return _has_provisional_d62_formula(workbook_path) if workbook_path.is_file() else False


DETAILED_RUN_STATE_SCHEMA = "airline-valuation-detailed-run-state/1"
DETAILED_RUN_OUTCOME = "prepared_uncalculated"
DETAILED_RUN_FILES = {"candidate.xlsx", "decision-report.json", "ledger.json", "package.json", RUN_STATE}


def _inspect_detailed_run(run_dir: Path, workbook_path: Path, state: dict[str, Any] | None,
                          state_errors: list[str]) -> int:
    """Validate and report a Detailed candidate without implying calculation or review."""
    from .detailed_workflow import load_contract

    issues = list(state_errors)
    report: dict[str, Any] = {
        "schema_version": "airline-valuation-detailed-inspect-report/1",
        "status": DETAILED_RUN_OUTCOME,
        "saved_workbook": str(workbook_path),
        "saved_sha256": None,
        "candidate_sha256": None,
        "decision_report_sha256": None,
        "ledger_sha256": None,
        "template_sha256": None,
        "package_sha256": None,
        "workbook_inventory": None,
        "issues": issues,
        "limitations": [
            "This is a structural inspection of a saved copy; it does not establish Excel calculation or recalculation.",
            "No reviewer record or attestation is created or validated by Detailed inspect.",
            "Cached values are observations only and are not treated as calculated valuation results.",
        ],
    }
    try:
        if run_dir.is_symlink() or not run_dir.is_dir():
            raise ValueError("Detailed run directory must be an existing regular directory, not a symlink")
        if {item.name for item in run_dir.iterdir()} != DETAILED_RUN_FILES:
            raise ValueError("Detailed run artifacts do not match the exact candidate package; copied legacy or extra files are not accepted")
        for name in DETAILED_RUN_FILES:
            path = run_dir / name
            if path.is_symlink() or not path.is_file():
                raise ValueError(f"unsafe or missing Detailed run artifact: {name}")
        if state is None:
            raise ValueError("Detailed run-state is unavailable")
        if state.get("schema") != DETAILED_RUN_STATE_SCHEMA:
            raise ValueError("Detailed run-state schema is not supported")
        if state.get("outcome") != DETAILED_RUN_OUTCOME:
            raise ValueError("Detailed run-state outcome must remain prepared_uncalculated")
        candidate = run_dir / "candidate.xlsx"
        decision_path = run_dir / "decision-report.json"
        ledger_path = run_dir / "ledger.json"
        package_path = run_dir / "package.json"
        if ".." in workbook_path.parts:
            raise ValueError("saved workbook path must not contain traversal components")
        for parent in (workbook_path, *workbook_path.parents):
            if parent.is_symlink():
                raise ValueError(f"saved workbook path contains a symlink: {parent}")
        if not workbook_path.is_file():
            raise ValueError("saved workbook must be a regular file")
        report["saved_sha256"] = sha256(workbook_path)
        report["candidate_sha256"] = sha256(candidate)
        report["saved_matches_candidate"] = report["saved_sha256"] == report["candidate_sha256"]
        expected_saved_hash = state.get("saved_copy_sha256")
        if expected_saved_hash is not None and expected_saved_hash != report["saved_sha256"]:
            issues.append("saved workbook hash differs from optional run-state saved_copy_sha256 binding")
        report["decision_report_sha256"] = sha256(decision_path)
        report["ledger_sha256"] = sha256(ledger_path)
        report["package_file_sha256"] = sha256(package_path)
        hash_bindings = {
            "candidate_sha256": report["candidate_sha256"],
            "decision_report_sha256": report["decision_report_sha256"],
            "ledger_sha256": report["ledger_sha256"],
            "package_file_sha256": report["package_file_sha256"],
        }
        for key, digest in hash_bindings.items():
            if state.get(key) != digest:
                issues.append(f"{key} differs from Detailed run-state binding")
        decision, _, decision_errors = load_json(decision_path)
        ledger, _, ledger_errors = load_json(ledger_path)
        package, _, package_errors = load_json(package_path)
        issues.extend(decision_errors)
        issues.extend(ledger_errors)
        issues.extend(package_errors)
        if decision is None or ledger is None or package is None:
            raise ValueError("Detailed decision report, ledger, or package is unreadable")
        from .detailed_workflow import (
            _synthetic_decision_package,
            _validate_synthetic_test_package,
            canonical_digest,
        )
        if not isinstance(package.get("cells"), dict):
            raise ValueError("Detailed package does not contain a cell-decision object")
        if package.get("schema_version") != "airline-valuation-detailed-synthetic-test-package/1":
            issues.append("Detailed package schema is not supported for prepared_uncalculated runs")
        try:
            _validate_synthetic_test_package(package, load_contract())
            expected_synthetic_package = _synthetic_decision_package()
            if package != expected_synthetic_package:
                raise ValueError("package differs from deterministic test fixture")
        except (KeyError, TypeError, ValueError) as exc:
            issues.append(f"Detailed synthetic package provenance validation failed: {exc}")
        package_digest = canonical_digest(package)
        report["package_sha256"] = package_digest
        if state.get("package_sha256") != package_digest or decision.get("decision_package_sha256") != package_digest:
            issues.append("Detailed decision package hash differs from report/run-state binding")
        if decision.get("decision_package") != package:
            issues.append("Detailed decision report package differs from package.json")
        if decision.get("canonical_sha256") != canonical_digest({key: value for key, value in decision.items() if key != "canonical_sha256"}):
            issues.append("Detailed decision report canonical hash is invalid")
        if ledger.get("schema") != "airline-valuation-detailed-run-ledger/1":
            issues.append("Detailed ledger schema is not supported")
        if ledger.get("candidate_sha256") != report["candidate_sha256"] or ledger.get("package_sha256") != package_digest:
            issues.append("Detailed ledger candidate/package binding is invalid")
        contract = load_contract()
        template = Path(str(state.get("template_path", "")))
        if template.is_symlink() or not template.is_file():
            issues.append("original Detailed template is missing or unsafe")
        else:
            report["template_sha256"] = sha256(template)
            if report["template_sha256"] != contract.get("sha256") or state.get("template_sha256") != report["template_sha256"]:
                issues.append("original template hash differs from Detailed contract/run-state")
        allowlisted = set(contract.get("input_allowlist", []))
        package_cells = package.get("cells", {})
        if set(package_cells) - allowlisted:
            issues.append("Detailed package contains unauthorized input references: " + ", ".join(sorted(set(package_cells) - allowlisted)))
        entries = {ref: str(value.get("decision")) for ref, value in package_cells.items()
                   if isinstance(ref, str) and isinstance(value, dict) and value.get("decision") not in (None, "")}
        if not set(contract.get("required_inputs", [])) <= set(entries):
            issues.append("Detailed decision package does not bind every required contract input")
        if ledger.get("cells") != entries:
            issues.append("Detailed ledger values differ from the decision package")
        if template.is_file() and not template.is_symlink() and set(entries) == set(contract.get("required_inputs", [])):
            try:
                from .provisional_writer import verify_provisional_delta
                conversions = {ref: entries[ref] for ref in contract.get("formula_to_numeric_inputs", {})
                               if ref in entries}
                verify_provisional_delta(template, candidate, entries, conversions, contract=contract)
            except (OSError, ValueError, WorkbookContractError, zipfile.BadZipFile) as exc:
                issues.append(f"candidate workbook authorized Detailed delta invalid: {exc}")
        # Compare every allowlisted input in the submitted copy with the
        # packaged candidate. Package-declared inputs must also match both the
        # package and ledger; all other allowlisted cells must remain unchanged.
        try:
            detailed_contract_path = Path(__file__).with_name("detailed_contract.json")
            with Workbook(candidate, detailed_contract_path) as candidate_wb, Workbook(workbook_path, detailed_contract_path) as saved_wb:
                allowed_refs = contract.get("input_allowlist", [])
                if not isinstance(allowed_refs, list) or len(set(allowed_refs)) != len(allowed_refs):
                    issues.append("Detailed contract input allowlist is malformed")
                for ref in allowed_refs:
                    if not isinstance(ref, str) or "!" not in ref:
                        issues.append(f"invalid Detailed allowlisted input reference: {ref!r}")
                        continue
                    sheet, cell = ref.rsplit("!", 1)
                    try:
                        candidate_value = candidate_wb.read_input(cell, sheet)
                    except (KeyError, PermissionError, ValueError, WorkbookContractError) as exc:
                        issues.append(f"candidate allowlisted input missing/unreadable: {ref}: {exc}")
                        continue
                    try:
                        saved_value = saved_wb.read_input(cell, sheet)
                    except (KeyError, PermissionError, ValueError, WorkbookContractError) as exc:
                        issues.append(f"saved allowlisted input missing/unreadable: {ref}: {exc}")
                        continue
                    expected_value = entries.get(ref, candidate_value)
                    metadata = contract.get("input_metadata", {}).get(ref, {})
                    if isinstance(expected_value, str) and metadata.get("kind") == "numeric":
                        try:
                            expected_value = Decimal(expected_value)
                        except InvalidOperation:
                            pass
                    if not _same_cell_value(candidate_value, expected_value):
                        issues.append(f"candidate allowlisted input differs from package/ledger: {ref}")
                    if not _same_cell_value(saved_value, expected_value):
                        issues.append(f"saved-copy input mismatch: {ref}")
        except (OSError, ValueError, KeyError, TypeError, PermissionError, WorkbookContractError, zipfile.BadZipFile) as exc:
            issues.append(f"Detailed saved-copy input comparison failed: {exc}")
        try:
            inventory = inspect_detailed_workbook(workbook_path, candidate=True)
            report["workbook_inventory"] = inventory
            if inventory.get("source_sha256") != report["saved_sha256"]:
                issues.append("saved workbook hash changed during Detailed inspection")
            if inventory.get("blocked") is not False or inventory.get("blockers"):
                issues.extend("active Detailed workbook diagnostic: " + str(reason) for reason in inventory.get("blockers", []))
            try:
                validate_verified_driver_evidence(inventory, candidate=True)
            except (ValueError, TypeError) as exc:
                issues.append(f"active Detailed formula/lookup/cache diagnostics: {exc}")
            cached_errors = inventory.get("cached_errors", {})
            candidate_d62 = (inventory.get("formulas", {}).get("D62", {}).get("formula")
                             == contract.get("d62", {}).get("provisional_formula"))
            expected_uncalculated_d62 = candidate_d62 and cached_errors.get("D62") == "#DIV/0!"
            report["cache_uncertainties"] = ([f"{ref} retains cached error {value}; candidate is uncalculated"
                                               for ref, value in sorted(cached_errors.items())]
                                              if candidate_d62 else [])
            unexpected_d62_cache = "D62" in cached_errors and not expected_uncalculated_d62
            if unexpected_d62_cache:
                issues.append("active Detailed route D62 cache differs from the named uncalculated candidate condition")
        except (OSError, ValueError, KeyError, TypeError, zipfile.BadZipFile, ET.ParseError) as exc:
            issues.append(f"Detailed saved-copy structural inspection failed: {exc}")
        report["status"] = DETAILED_RUN_OUTCOME
    except (OSError, ValueError, TypeError, KeyError, PermissionError) as exc:
        issues.append(str(exc))
    report["issues"] = list(dict.fromkeys(issues))
    report["valid"] = not report["issues"]
    # Keep the run package's exact artifact set intact; the diagnostic report is
    # emitted to stdout and never added as an unbound run artifact.
    print(json.dumps(report, indent=2, sort_keys=True, default=str))
    return 0 if report["valid"] else BLOCKED


def cmd_inspect(a: argparse.Namespace) -> int:
    run_dir, workbook_path = a.run_dir, a.workbook
    if run_dir.is_symlink():
        print(f"INSPECT BLOCKED: refusing symlink run directory: {run_dir}", file=sys.stderr)
        return BLOCKED
    early_state, _, early_errors = load_json(run_dir / RUN_STATE)
    if isinstance(early_state, dict) and early_state.get("schema") == DETAILED_RUN_STATE_SCHEMA:
        return _inspect_detailed_run(run_dir, workbook_path, early_state, early_errors)
    # This check must precede mkdir and every report/state write. Provisional
    # workbooks have no approved Excel-recalculation or attestation workflow.
    if run_dir.is_symlink():
        print(f"INSPECT BLOCKED: refusing symlink run directory: {run_dir}", file=sys.stderr)
        return BLOCKED
    state, _, _ = load_json(run_dir / RUN_STATE)
    if _is_provisional_run(run_dir, state, workbook_path):
        print("INSPECT BLOCKED: provisional workbooks cannot enter approved inspection or attestation.", file=sys.stderr)
        return BLOCKED
    run_dir.mkdir(parents=True, exist_ok=True)
    state_path = run_dir / RUN_STATE
    state, _, state_errors = load_json(state_path)
    issues = list(state_errors)
    status_code = MISSING if state_errors and state_errors[0].startswith("missing file:") else BLOCKED
    report: dict[str, Any] = {"status": "unverified", "attestation_outcome": "missing", "source_manifest": None,
        "manifest_sha256": None, "prepared_sha256": None, "saved_workbook": str(workbook_path), "saved_sha256": None,
        "valuation_cutoff": None, "saved_cell_references": {}, "source_ledger": [], "assumptions": [],
        "diagnostics": [], "issues": issues, "limitations": ["Values are only read from saved cached cells; no calculation or recalculation is performed.", "A human attestation is not cryptographic proof of signer identity.", "No valuation is independently calculated or validated."]}
    if workbook_path.is_file():
        try: report["saved_sha256"] = sha256(workbook_path)
        except OSError as exc: issues.append(f"cannot hash saved workbook: {exc}")
    else:
        issues.append(f"missing workbook: {workbook_path}")
        status_code = MISSING

    manifest = None
    raw_manifest = None
    prepared = run_dir / "prepared.xlsx"
    if state is not None:
        manifest_path = Path(str(state.get("manifest_path", "")))
        report["source_manifest"] = str(manifest_path)
        report["valuation_cutoff"] = state.get("valuation_cutoff")
        manifest, raw_manifest, manifest_errors = load_json(manifest_path)
        issues.extend(manifest_errors)
        prepared = Path(state.get("prepared_path") or prepared)
        try:
            if prepared.resolve().parent != run_dir.resolve():
                issues.append("prepared workbook path escapes the run directory")
        except OSError as exc:
            issues.append(f"cannot resolve prepared workbook path: {exc}")
    else:
        issues.extend(state_errors or ["run state is unavailable"])
    if manifest is not None and raw_manifest is not None:
        report["manifest_sha256"] = hashlib.sha256(raw_manifest).hexdigest()
        report["valuation_cutoff"] = manifest.get("cutoff")
        report["assumptions"] = manifest.get("analyst_assumptions", [])
        report["source_ledger"] = [{"id": item.get("id"), "source": _citation(item), "workbook_cells": _refs(item)} for group in ("facts", "market_observations") for item in manifest.get(group, []) if isinstance(item, dict)]
        validation = validate_manifest(manifest)
        readiness = assess_readiness(manifest)
        if not validation.valid:
            issues.extend("manifest invalid: " + e for e in validation.errors)
        elif not readiness.ready:
            issues.extend("manifest not ready: " + e for e in readiness.blockers)

    att, _, att_errors = load_json(run_dir / ATTESTATION)
    if att_errors:
        issues.extend(att_errors)
    saved_hash = report["saved_sha256"]
    if state is not None:
        if report["manifest_sha256"] and state.get("manifest_sha256") != report["manifest_sha256"]:
            issues.append("manifest hash differs from run-state binding")
        report["prepared_sha256"] = state.get("prepared_sha256")
        if state.get("saved_sha256") and saved_hash and state.get("saved_sha256") != saved_hash:
            issues.append("saved workbook hash differs from prior recorded inspect outcome")
    if att is not None and saved_hash:
        issues.extend(_attestation_errors(att, {"manifest_sha256": report["manifest_sha256"] or "", "prepared_sha256": report["prepared_sha256"] or "", "saved_sha256": saved_hash}))
        report["attestation_outcome"] = "signed-declaration-valid" if not _attestation_errors(att, {"manifest_sha256": report["manifest_sha256"] or "", "prepared_sha256": report["prepared_sha256"] or "", "saved_sha256": saved_hash}) else "invalid-or-stale"
    else:
        report["attestation_outcome"] = "missing-or-unreadable"

    manifest_gate_blocked = manifest is None or (manifest is not None and (not validate_manifest(manifest).valid or not assess_readiness(manifest).ready))
    contract = None
    if workbook_path.is_file() and prepared.is_file():
        try:
            contract = _contract()
            if state is not None:
                template = Path(state.get("template_path", Path(__file__).resolve().parents[2] / contract["source_file"]))
                if not template.is_file(): issues.append(f"original template missing: {template}")
                else:
                    original_hash = sha256(template)
                    if original_hash != contract.get("sha256") or original_hash != state.get("template_sha256"):
                        issues.append("original template hash differs from contract/run-state")
            if state is not None and sha256(prepared) != state.get("prepared_sha256"):
                issues.append("prepared workbook hash differs from run-state")
            prepared_inputs = {}
            normalized_entries: dict[str, str] = {}
            for ref, _raw_value, item, _ in mappings(manifest) if manifest is not None else []:
                try:
                    normalized_value, _target_unit = _normalize_mapped_entry(ref, item, contract.get("input_metadata", {}).get(ref, {}))
                    normalized_entries[ref] = str(normalized_value)
                except (TypeError, ValueError, KeyError) as exc:
                    issues.append(f"cannot normalize approved input {ref}: {exc}")
            try:
                verify_prepared_delta(template, prepared, normalized_entries)
            except (OSError, ValueError, WorkbookContractError, zipfile.BadZipFile) as exc:
                issues.append(f"prepared workbook authorized delta invalid: {exc}")
            ledger_path = run_dir / "ledger.jsonl"
            try:
                ledger_records = [json.loads(line) for line in ledger_path.read_text(encoding="utf-8").splitlines() if line.strip()]
                ledger_items = {item.get("cell"): item for record in ledger_records for item in record.get("items", []) if isinstance(item, dict)}
                for ref, value in normalized_entries.items():
                    if ledger_items.get(ref, {}).get("normalized_value") != value:
                        issues.append(f"normalized ledger mismatch: {ref}")
                if not ledger_records or state is None or any(record.get("manifest_sha256") != state.get("manifest_sha256") for record in ledger_records):
                    issues.append("ledger manifest hash differs from run-state")
            except (OSError, ValueError, TypeError) as exc:
                issues.append(f"cannot verify normalized ledger: {exc}")
            with Workbook(prepared) as p_wb:
                try:
                    p_wb.verify_structure(mode="saved")
                except WorkbookContractError as exc:
                    issues.append(f"prepared workbook structure invalid: {exc}")
                for ref in contract.get("input_allowlist", []):
                    if not isinstance(ref, str) or "!" not in ref:
                        issues.append(f"invalid contract input allowlist entry: {ref!r}")
                        continue
                    sheet, cell = ref.rsplit("!", 1)
                    try:
                        prepared_inputs[ref] = p_wb.read_input(cell, sheet)
                    except (KeyError, PermissionError, ValueError, WorkbookContractError) as exc:
                        issues.append(f"cannot read prepared input {ref}: {exc}")
            if sha256(workbook_path) == sha256(prepared):
                issues.append("saved workbook is identical to prepared workbook; Excel save/recalculation is absent")
            with Workbook(workbook_path) as wb:
                if wb.verify_identity().get("matches_template"):
                    issues.append("saved workbook is byte-identical to pristine template; Excel save/input changes are absent")
                try:
                    report["structure"] = wb.verify_structure(mode="saved")
                except WorkbookContractError as exc:
                    issues.append(f"saved workbook structure invalid: {exc}")
                selected_branch = wb.branch_status().get("branch", "unsupported")
                diagnostics: dict[str, Any] = {}
                diagnostic_specs = contract.get("conditional_diagnostics_by_branch", {}).get(selected_branch, [])
                if not isinstance(diagnostic_specs, list):
                    issues.append(f"conditional diagnostics for {selected_branch} are not a list")
                    diagnostic_specs = []
                for spec in diagnostic_specs:
                    if not isinstance(spec, dict) or not isinstance(spec.get("cell"), str) or "!" not in spec["cell"]:
                        issues.append("invalid conditional diagnostic specification; skipped")
                        continue
                    ref = spec["cell"]
                    sheet, cell = ref.rsplit("!", 1)
                    try:
                        value = wb.read_output(cell, sheet)
                    except (KeyError, PermissionError, ValueError) as exc:
                        issues.append(f"cannot read active diagnostic {ref}: {exc}")
                        continue
                    dtype = spec.get("type")
                    diagnostics[ref] = {"saved_value": value, "type": dtype, "required": spec.get("required", False)}
                    report["saved_cell_references"][ref] = diagnostics[ref]
                    if isinstance(value, str) and value.startswith("#"):
                        issues.append(f"cached Excel diagnostic error at {ref}: {value}")
                    if spec.get("required") and dtype == "numeric" and not _finite_number(value):
                        issues.append(f"required active diagnostic missing/non-finite: {ref}")
                report["diagnostics"] = diagnostics
                input_refs, input_issues = _compare_allowlisted_inputs(
                    contract, prepared_inputs, wb.read_input, manifest
                )
                report["saved_cell_references"].update(input_refs)
                issues.extend(input_issues)
                branch = wb.branch_status()
                report["branch_status"] = branch
                if branch.get("ready") is not True: issues.append("branch_status.ready is not true")
                if branch.get("branch") != "direct_input": issues.append("saved workbook is not on direct_input branch")
                for key in ("unsupported_branches", "type_failures", "unresolved_switches", "missing_required_inputs", "cached_error_outputs"):
                    if branch.get(key): issues.append(f"{key}: " + ", ".join(map(str, branch[key])))
                for ref, spec in branch.get("active_outputs", {}).items():
                    sheet, cell = ref.rsplit("!", 1)
                    value = wb.read_output(cell, sheet)
                    report["saved_cell_references"][ref] = {"saved_value": value, "type": spec.get("type"), "required": spec.get("required")}
                    if spec.get("required") and spec.get("type") == "numeric" and not _finite_number(value): issues.append(f"required output missing/non-finite: {ref}")
                issues.extend(_identities(wb))
        except (OSError, ValueError, KeyError, TypeError, PermissionError, WorkbookContractError, zipfile.BadZipFile) as exc:
            issues.append(f"workbook structure/read error: {exc}")
    elif not prepared.is_file() and not manifest_gate_blocked:
        issues.append(f"missing prepared workbook: {prepared}")
        status_code = MISSING
    else:
        issues.append("saved workbook unavailable for structural inspection")

    issues = list(dict.fromkeys(issues))
    report["issues"] = issues
    report["status"] = "Excel-attested" if not issues else "unverified"
    report["report_path"] = str((run_dir / "inspect-report.json").resolve())
    report["summary_path"] = str((run_dir / "summary.json").resolve())
    try:
        _persist_inspection(run_dir, report, state_path if state is not None else None, state)
    except OSError as exc:
        issues.append(f"failed to persist inspect report/run-state: {exc}")
        report["issues"] = issues
        report["status"] = "unverified"
        status_code = BLOCKED
        try: _persist_inspection(run_dir, report)
        except OSError: pass
    print(json.dumps(report, indent=2, ensure_ascii=False))
    if report["status"] != "Excel-attested":
        return MISSING if status_code == MISSING else BLOCKED
    return 0


PROVISIONAL_STATUS = "PROVISIONAL — NOT APPROVED — NOT CALCULATED"


def _validate_provisional_stage(stage: Path, template: Path, cells: dict[str, str],
                                scenario_hash: str, state: dict[str, Any],
                                formula_to_numeric_inputs: dict[str, str]) -> None:
    required = {"provisional.xlsx", "NOTICE.txt", "ledger.json", RUN_STATE}
    if {p.name for p in stage.iterdir()} != required or any(
        not (stage / name).is_file() or (stage / name).is_symlink() for name in required
    ):
        raise ValueError("provisional stage is incomplete or contains unexpected artifacts")
    from .detailed_inventory import validate_input_formula_dependents
    validate_input_formula_dependents(template)
    validate_input_formula_dependents(stage / "provisional.xlsx", staged_values={
        ref: cells[ref] for ref in ("Input sheet!B27", "Input sheet!B28", "Input sheet!B32")
    })
    verify_provisional_delta(template, stage / "provisional.xlsx", cells,
                             {ref: cells[ref] for ref in formula_to_numeric_inputs})
    if state.get("output_sha256") != sha256(stage / "provisional.xlsx") or state.get("template_sha256") != sha256(template):
        raise ValueError("staged provisional workbook/template hashes differ from run-state")
    ledger, _, errors = load_json(stage / "ledger.json")
    if errors or not ledger or ledger.get("scenario_sha256") != scenario_hash or ledger.get("status") != PROVISIONAL_STATUS:
        raise ValueError("staged provisional ledger is unreadable or unbound")
    if ledger.get("cells") != cells or ledger.get("output_sha256") != state["output_sha256"]:
        raise ValueError("staged provisional ledger differs from populated inputs or output hash")
    saved_state, _, errors = load_json(stage / RUN_STATE)
    if errors or saved_state != state or PROVISIONAL_STATUS not in (stage / "NOTICE.txt").read_text(encoding="utf-8"):
        raise ValueError("staged provisional state or notice is inconsistent")


def _require_existing_run_class(path: Path, *, provisional: bool) -> None:
    _validate_existing_run(path)
    expected = ({"provisional.xlsx", "NOTICE.txt", "ledger.json", RUN_STATE} if provisional else
                {"prepared.xlsx", "checklist.txt", "ledger.jsonl", RUN_STATE})
    if {item.name for item in path.iterdir()} != expected:
        raise ValueError("refusing overwrite: existing run artifacts do not match the requested workflow")
    state, _, errors = load_json(path / RUN_STATE)
    schema = "airline-valuation-provisional-run-state/1" if provisional else "airline-valuation-run-state/1"
    if errors or state is None or state.get("schema") != schema:
        raise ValueError("refusing overwrite: existing run-state schema does not match the requested workflow")


def cmd_provisional(a: argparse.Namespace) -> int:
    """Stage a private, uncalculated workbook only after the pinned route audit passes."""
    template = Path(__file__).resolve().parents[2] / "fcffsimpleginzu.xlsx"
    try:
        evidence = inspect_detailed_workbook(template)
        validate_verified_driver_evidence(evidence)
        from .detailed_inventory import validate_input_formula_dependents
        validate_input_formula_dependents(template)
        scenario_path = Path(a.scenario)
        scenario = load_provisional_scenario(scenario_path)
        cells = scenario["cells"]
        outdir = a.output_dir
        _reject_symlink_path(outdir)
        parent = outdir.parent
        parent.mkdir(parents=True, exist_ok=True)
        backups = sorted(parent.glob(f".{outdir.name}.backup-*"))
        if backups:
            raise ValueError(f"orphan run backup(s) detected: {backups}; manually inspect and restore before retry")
        if outdir.exists() and not a.overwrite:
            raise FileExistsError(f"run directory already exists: {outdir}; pass --overwrite")
        if outdir.exists():
            _require_existing_run_class(outdir, provisional=True)
        stage = Path(tempfile.mkdtemp(prefix=f".{outdir.name}.stage-", dir=parent))
        backup = parent / f".{outdir.name}.backup-{os.getpid()}"
        try:
            if backup.exists() or backup.is_symlink():
                raise FileExistsError(f"ambiguous backup path exists: {backup}")
            workbook = stage / "provisional.xlsx"
            conversions = {ref: cells[ref] for ref in scenario["formula_to_numeric_inputs"]}
            write_provisional(template, workbook, cells, conversions)
            verify_provisional_delta(template, workbook, cells, conversions)
            scenario_hash = sha256(scenario_path)
            output_hash = sha256(workbook)
            unresolved = {
                "debt_maturity": "Cost of capital worksheet!B32=0 is the workbook comment fallback because Note 6 reports no single average debt maturity; the issuer's average maturity remains unresolved.",
                "excel_repair": "Excel repair log error076040_02.xml reports removal of a FORMULA RECORD from xl/calcChain.xml, not removal of the entire file; the older Excel-saved copy is not attested and repair impact remains unresolved.",
                "input_b32_sales_to_capital": "Input sheet!B32=1.25 is the illustrative years 6–10 sales-to-capital ratio, not debt maturity.",
                "rating": "Baa2/BBB is mapped from issuer rating evidence as of 2025-12-31; source-to-dropdown mapping remains provisional and requires review.",
                "lookup_tables": "Embedded industry, spread and ERP tables are assumed suitable for private provisional use; currency is not verified.",
                "geography": "FY2025 Form 10-K PDF pp. 100-101 reports one operating segment and no geographic allocation; issuer geographic revenue distribution remains unresolved.",
                "regional_allocation": "North-America-only allocation is an operator-defined scenario assumption, not reported regional revenue.",
                "B19_nonoperating": "B19=0 is an analyst classification that excludes 457 million USD equity securities primarily linked to deferred compensation; classification remains unresolved.",
                "B20_noncontrolling_interest": "B20=0 is an analyst judgment; no separate NCI line does not prove NCI absence.",
                "forecast_assumptions": "Input sheet!B27 is an illustrative next-year pre-tax operating margin, B28 is illustrative long-term revenue growth, and B29 is an illustrative long-term target pre-tax operating margin. No GAAP margin is inferred from adjusted EPS.",
                "price_rights_cutoff": "Yahoo unadjusted February 6, 2026 close is personal-use only; price availability at cutoff and redistribution rights remain unresolved.",
                "excel_review": "Stale cached values are not calculations; desktop Excel recalculation and human review are required, and recalculation is deferred by the operator.",
            }
            notice = (PROVISIONAL_STATUS + "\nPrivate use only. This workbook has not been calculated; do not claim a calculated WACC or valuation. Existing Excel caches may be stale or contain errors.\n"
                      "Keep this NOTICE and ledger alongside any detached XLSX; a detached copy cannot carry an enforceable warning.\n"
                      "Excel repair log error076040_02.xml reports removal of a FORMULA RECORD from xl/calcChain.xml, not removal of the entire file; the older Excel-saved copy is not attested.\n"
                      "Unresolved: B19 treatment of 457 million USD equity securities, B20 noncontrolling interests, issuer geographic revenue (not allocated in the filing), Note 6 average debt maturity (B32=0 comment fallback), industry/spread/ERP lookup interpretation, price rights/cutoff, and Excel recalculation/review.\n"
                      "Desktop Excel recalculation, review of assumptions/tables, price terms and human acceptance are required before any calculated result. No calculated WACC is supplied.\n")
            (stage / "NOTICE.txt").write_text(notice, encoding="utf-8")
            ledger = {"status": PROVISIONAL_STATUS, "template_sha256": sha256(template), "scenario_sha256": scenario_hash,
                      "output_sha256": output_hash, "cells": cells, "units": scenario["units"],
                      "provenance": scenario["provenance"], "uncertainties": scenario["uncertainties"],
                      "unresolved": unresolved, "candidate_dispositions": scenario.get("candidate_dispositions", {}),
                      "candidate_inventory": scenario.get("candidate_inventory", {}),
                      "formula_to_numeric_inputs": {ref: {**record, "value": cells[ref]} for ref, record in scenario["formula_to_numeric_inputs"].items()},
                      "judgments": {ref: scenario["provenance"][ref] for ref in cells},
                      "replacements": {ref: {"from": "pinned formula and cached value", "to": cells[ref]}
                                       for ref in scenario["formula_to_numeric_inputs"]}}
            (stage / "ledger.json").write_text(json.dumps(ledger, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
            state = {"schema": "airline-valuation-provisional-run-state/1", "outcome": PROVISIONAL_STATUS,
                     "scenario_sha256": scenario_hash, "template_sha256": sha256(template),
                     "output_sha256": output_hash, "unresolved": unresolved}
            (stage / RUN_STATE).write_text(json.dumps(state, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
            _validate_provisional_stage(stage, template, cells, scenario_hash, state,
                                        scenario["formula_to_numeric_inputs"])
            moved_old = False
            try:
                if outdir.exists():
                    os.replace(outdir, backup)
                    moved_old = True
                os.replace(stage, outdir)
            except OSError:
                if moved_old and not outdir.exists():
                    try:
                        os.replace(backup, outdir)
                    except OSError as rollback_error:
                        raise OSError(f"publication failed and backup restoration failed; previous run remains at {backup}: {rollback_error}")
                raise
            if moved_old:
                try:
                    shutil.rmtree(backup)
                except OSError as exc:
                    print(f"WARNING: new provisional run published; prior backup remains at {backup}: {exc}", file=sys.stderr)
        finally:
            if stage.exists():
                shutil.rmtree(stage)
    except (OSError, ValueError, TypeError, KeyError, zipfile.BadZipFile, ET.ParseError) as exc:
        print(f"PROVISIONAL BLOCKED: {exc}", file=sys.stderr)
        return BLOCKED
    print(f"{PROVISIONAL_STATUS}\nWorkbook: {outdir / 'provisional.xlsx'}\nLedger: {outdir / 'ledger.json'}")
    return 0


def cmd_detailed_analyze(a: argparse.Namespace) -> int:
    report = detailed_analyze(a.evidence_bundle, a.output_dir, policy_version=a.policy_version)
    print(json.dumps(report, indent=2, sort_keys=True))
    # A valid report is a successful analysis even when it documents unresolved inputs.
    return 0


def cmd_filing_import(a: argparse.Namespace) -> int:
    provenance_path = a.provenance or (a.metadata_dir.parent / "provenance.json")
    if not provenance_path.exists():
        provenance_path = None
    path = import_filings(a.metadata_dir, a.document_dir, a.output_dir, cik=a.cik, accession=a.accession, provenance_path=provenance_path)
    print(f"validated filing archive: {path}")
    return 0


def cmd_filing_analyze(a: argparse.Namespace) -> int:
    result = analyze_archive(a.archive_dir, a.output_dir)
    print(f"created offline filing reports in {result}")
    return 0


def cmd_filing_analyze_test_fixture(a: argparse.Namespace) -> int:
    result = analyze_archive_test_fixture(a.archive_dir, a.output_dir)
    print(f"created TEST ONLY offline filing reports in {result}")
    return 0


def cmd_filing_acquire(a: argparse.Namespace) -> int:
    accession_digits = a.accession.replace("-", "")
    matches = [(name, filing) for name, filing in FILINGS.items()
               if filing["cik"].lstrip("0") == str(a.cik).lstrip("0") and filing["accession"] == a.accession]
    if not matches or not a.user_agent:
        raise ValueError("acquisition requires a registered CIK/accession and identifying User-Agent")
    name, filing = matches[0]
    client = SECFilingClient(a.user_agent)
    responses = {}
    provenance = {"schema": "airline-sec-acquisition/1", "rights": "unknown; provenance does not grant redistribution rights", "filing": {"cik": filing["cik"], "accession": filing["accession"]}, "artifacts": {}}
    for kind, spec in filing["artifacts"].items():
        result = client.acquire(spec["url"])
        responses[kind] = result.body
        provenance["artifacts"][kind] = {"artifact_url": result.url, "actual_response_url": result.actual_response_url, "retrieved_at": result.retrieved_at.isoformat(), "byte_length": len(result.body), "content_type": result.content_type, "sha256": result.sha256}
    a.output_dir.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=f".{a.output_dir.name}.acquire-", dir=a.output_dir.parent))
    try:
        metadata = stage / "metadata"
        documents = stage / "documents"
        metadata.mkdir(); documents.mkdir()
        for kind, spec in filing["artifacts"].items():
            target = documents if kind == "primary" else metadata
            (target / spec["filename"]).write_bytes(responses[kind])
        (stage / "provenance.json").write_text(json.dumps(provenance, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(stage, a.output_dir)
    except Exception:
        shutil.rmtree(stage, ignore_errors=True)
        raise
    print(f"acquired registered response bodies for {name}; run filing-import to validate")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="valuation", description="Manual Excel valuation workflow; never calculates valuation outputs.")
    sub = p.add_subparsers(dest="command", required=True)
    r = sub.add_parser("review"); r.add_argument("--manifest", type=Path, required=True); r.add_argument("--output-dir", type=Path, required=True); r.add_argument("--overwrite", action="store_true"); r.set_defaults(func=cmd_review)
    v = sub.add_parser("validate"); v.add_argument("--manifest", type=Path, required=True); v.set_defaults(func=cmd_validate)
    prep = sub.add_parser("prepare"); prep.add_argument("--manifest", type=Path, required=True); prep.add_argument("--output-dir", type=Path, required=True); prep.add_argument("--overwrite", action="store_true"); prep.set_defaults(func=cmd_prepare)
    i = sub.add_parser("inspect"); i.add_argument("--run-dir", type=Path, required=True); i.add_argument("--workbook", type=Path, required=True); i.set_defaults(func=cmd_inspect)
    provisional = sub.add_parser("provisional", help="private, uncalculated Detailed-route workbook; not approved")
    provisional.add_argument("--scenario", type=Path, required=True)
    provisional.add_argument("--output-dir", type=Path, required=True)
    provisional.add_argument("--overwrite", action="store_true")
    provisional.set_defaults(func=cmd_provisional)
    detailed = sub.add_parser("detailed-analyze", help="evidence bundle to a fail-closed Detailed decision report")
    detailed.add_argument("--evidence-bundle", type=Path, required=True)
    detailed.add_argument("--policy-version", required=True)
    detailed.add_argument("--output-dir", type=Path, required=True)
    detailed.set_defaults(func=cmd_detailed_analyze)
    filing_import = sub.add_parser("filing-import", help="validate locally supplied SEC filing response bodies against code-owned pins")
    filing_import.add_argument("--metadata-dir", type=Path, required=True)
    filing_import.add_argument("--document-dir", type=Path, required=True)
    filing_import.add_argument("--output-dir", type=Path, required=True)
    filing_import.add_argument("--cik")
    filing_import.add_argument("--accession")
    filing_import.add_argument("--provenance", type=Path)
    filing_import.set_defaults(func=cmd_filing_import)
    filing_analyze = sub.add_parser("filing-analyze", help="analyze a verified local SEC archive without workbook promotion")
    filing_analyze.add_argument("--archive-dir", type=Path, required=True)
    filing_analyze.add_argument("--output-dir", type=Path, required=True)
    filing_analyze.set_defaults(func=cmd_filing_analyze)
    filing_test = sub.add_parser("filing-analyze-test-fixture", help="TEST ONLY report using fixed synthetic permission fixture; never real clearance")
    filing_test.add_argument("--archive-dir", type=Path, required=True)
    filing_test.add_argument("--output-dir", type=Path, required=True)
    filing_test.set_defaults(func=cmd_filing_analyze_test_fixture)
    filing_acquire = sub.add_parser("filing-acquire", help="explicitly acquire registered SEC filing resources")
    filing_acquire.add_argument("--cik", required=True)
    filing_acquire.add_argument("--accession", required=True)
    filing_acquire.add_argument("--output-dir", type=Path, required=True)
    filing_acquire.add_argument("--user-agent", required=True)
    filing_acquire.set_defaults(func=cmd_filing_acquire)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except (OSError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return BLOCKED


if __name__ == "__main__":
    raise SystemExit(main())
