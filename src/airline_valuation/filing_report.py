"""Offline reports for registry-pinned SEC filing archives; never workbook inputs."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any

from .filing_eligibility import (PINNED_CUTOFF, _verified_filing,
                                 accounting_eligibility_test_fixture,
                                 assess_filing_eligibility)
from .filing_ixbrl import parse_filing
from .filing_policy import _reconcile_verified_parse, reconcile_parsed_filing
from .filing_registry import FILINGS, REGISTRY_VERSION

REPORT_VERSION = "airline-filing-analysis/1"
TEST_LEASE_SCHEMA = "airline-filing-test-only-lease-reconciliation/1"


def verify_test_only_lease_artifact(archive_dir: Path, artifact_bytes: bytes, expected_sha256: str) -> dict:
    """Rehydrate and verify a TEST ONLY lease artifact against pinned source bytes."""
    if hashlib.sha256(artifact_bytes).hexdigest() != expected_sha256:
        raise ValueError("test-only lease artifact digest mismatch")
    artifact = json.loads(artifact_bytes.decode("utf-8"))
    if artifact.get("schema") != TEST_LEASE_SCHEMA or artifact.get("issuer") != "southwest":
        raise ValueError("unrecognized test-only lease artifact")
    root = _safe_root(Path(archive_dir))
    verified, _ = _verified_filing(root, "southwest")
    raw = (root / "southwest-luv-20251231.htm").read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    if artifact.get("source_sha256") != digest or verified["source_sha256"] != digest:
        raise ValueError("test-only lease artifact source hash mismatch")
    parsed = parse_filing(raw, source_id="southwest")
    policy = reconcile_parsed_filing(raw, parsed, "southwest")
    lease = policy["reconciliations"].get("lease_liabilities")
    if lease is None or artifact.get("lease_reconciliation") != lease:
        raise ValueError("test-only lease reconciliation does not match rehydrated source")
    return artifact


def _safe_root(path: Path) -> Path:
    path = Path(os.path.abspath(path))
    for part in (path, *path.parents):
        if part.is_symlink():
            raise ValueError(f"symlink path component is not allowed: {part}")
    if not path.is_dir():
        raise ValueError(f"archive directory does not exist: {path}")
    return path


def _validate_archive(root: Path) -> tuple[dict, dict[str, dict[str, bytes]]]:
    manifest_path = root / "manifest.json"
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise ValueError("archive manifest must be a regular file")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    filings = manifest.get("filings") if isinstance(manifest, dict) else None
    if not isinstance(filings, dict) or not filings:
        raise ValueError("archive has no complete registered filings")
    if manifest.get("schema") != REGISTRY_VERSION:
        raise ValueError("archive manifest schema is not trusted")
    allowed = {"manifest.json"}
    verified = {}
    for name in filings:
        if name not in FILINGS:
            raise ValueError(f"unregistered issuer in archive: {name}")
        _verified_filing(root, name)
        issuer = FILINGS[name]
        names = {f"{name}-submissions.json", f"{name}-index.json", f"{name}-{issuer['primary_document']}"}
        allowed.update(names)
        verified[name] = {kind: (root / filename).read_bytes() for kind, filename in (
            ("submissions", f"{name}-submissions.json"), ("index", f"{name}-index.json"),
            ("primary", f"{name}-{issuer['primary_document']}"))}
    for entry in root.iterdir():
        if entry.is_symlink() or not entry.is_file():
            raise ValueError("archive contains symlink or non-file entry")
        if entry.name not in allowed:
            raise ValueError(f"unexpected or path-traversal archive entry: {entry.name}")
    for name, record in filings.items():
        artifacts = record.get("artifacts", {})
        if set(artifacts) != {"submissions", "index", "primary"}:
            raise ValueError(f"partial artifact staging for {name}")
    return manifest, verified


def _atomic_json(path: Path, obj: dict) -> None:
    tmp = path.with_name("." + path.name + ".tmp")
    with tmp.open("x", encoding="utf-8") as stream:
        json.dump(obj, stream, indent=2, sort_keys=True, ensure_ascii=False)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(tmp, path)


def analyze_archive(archive_dir: Path, output_dir: Path) -> Path:
    """Create production reports from a verified archive; source rights remain unknown."""
    return _analyze_archive(archive_dir, output_dir, test_fixture=False)


def analyze_archive_test_fixture(archive_dir: Path, output_dir: Path) -> Path:
    """Create an explicitly TEST ONLY report using one fixed synthetic permission fixture."""
    if Path(output_dir).name.casefold().startswith("test-only-") is False:
        raise ValueError("test-fixture reports require an output directory named TEST-ONLY-...")
    return _analyze_archive(archive_dir, output_dir, test_fixture=True)


def _analyze_archive(archive_dir: Path, output_dir: Path, *, test_fixture: bool) -> Path:
    """Shared verified archive pipeline; fixture data is never caller-supplied."""
    root = _safe_root(archive_dir)
    output = Path(os.path.abspath(output_dir))
    for part in (output, *output.parents):
        if part.is_symlink():
            raise ValueError("symlink output path component is not allowed")
    if output.exists():
        raise FileExistsError(output)
    manifest, byte_groups = _validate_archive(root)
    output.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=f".{output.name}.stage-", dir=output.parent))
    try:
        reports: dict[str, dict[str, Any]] = {}
        ledger = []
        manifest_digest = hashlib.sha256((root / "manifest.json").read_bytes()).hexdigest()
        for name, artifacts in byte_groups.items():
            original = artifacts["primary"]
            digest = hashlib.sha256(original).hexdigest()
            parsed = parse_filing(original, source_id=name)
            # This locally-created parse is bound to the verified archive bytes; caller-provided parsed JSON must use the public revalidating API.
            candidates = [{key: fact.get(key) for key in (
                "id", "concept", "value", "raw_text", "context_ref", "context", "unit_ref", "unit", "locator", "unresolved")}
                for fact in parsed["facts"]]
            # Bind emitted observations to their exact archived source without reparsing.
            source_locators = {tuple(fact["locator"]["path"]): fact["locator"] for fact in parsed["facts"]}
            if any(source_locators.get(tuple(item["locator"]["path"])) != item["locator"] or
                   item["locator"].get("sha256") != digest or item["locator"].get("source_id") != name
                   for item in candidates):
                raise ValueError("parsed locator does not rehydrate against archived original bytes")
            observations_path = f"{name}-observations.json"
            observations = {"schema": "airline-filing-observations/1", "issuer": name,
                "source_sha256": digest, "registry_sha256": FILINGS[name]["artifacts"]["primary"]["sha256"],
                "candidates": candidates, "contexts": parsed["contexts"], "units": parsed["units"],
                "diagnostics": parsed["diagnostics"], "limitations": parsed["limitations"],
                "workbook_promotion": False}
            _atomic_json(stage / observations_path, observations)
            eligibility = assess_filing_eligibility(root, name)
            if name == "southwest":
                policy_result = _reconcile_verified_parse(original, parsed, name)
                reconciliations = policy_result["reconciliations"]
                issuer_status = "source-derived reconciliation; rights and first-public availability unresolved"
            else:
                policy_result = _reconcile_verified_parse(original, parsed, name)
                reconciliations = {}
                issuer_status = "extraction-only; no issuer reconciliation policy"
            report = {"schema": REPORT_VERSION, "issuer": name, "status": issuer_status,
                "source_format": "html_inline_xbrl",
                "source": {"artifact_url": FILINGS[name]["artifacts"]["primary"]["url"],
                    "sha256": digest, "registry_sha256": FILINGS[name]["artifacts"]["primary"]["sha256"],
                    "manifest_sha256": manifest_digest, "source_bytes_verified": True,
                    "rights": "unknown", "first_public_availability": "unknown",
                    "accepted_at": eligibility.get("accepted_at"), "cutoff": PINNED_CUTOFF,
                    "retrieved_at": manifest["filings"][name]["artifacts"]["primary"].get("retrieved_at")},
                "eligibility": eligibility, "policy": {"version": policy_result["policy_version"],
                    "status": policy_result["status"], "exceptions": policy_result["exceptions"]},
                "reconciliations": reconciliations, "observations_file": observations_path,
                "observations_sha256": hashlib.sha256((stage / observations_path).read_bytes()).hexdigest(),
                "diagnostics": parsed["diagnostics"], "workbook_promotion": False, "valuation_claims": False}
            trace = None
            if name == "southwest" and test_fixture:
                trace = accounting_eligibility_test_fixture("synthetic-accounting-allowed")
                trace.pop("trace_token", None)
                if trace.get("eligible"):
                    lease = reconciliations.get("lease_liabilities")
                    if isinstance(lease, dict) and lease.get("status") == "reconciled":
                        lease_artifact = {"schema": TEST_LEASE_SCHEMA, "issuer": name,
                            "source_sha256": digest, "lease_reconciliation": lease,
                            "fixture_only": True, "production_source_clearance": False}
                        lease_bytes = (json.dumps(lease_artifact, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8")
                        lease_digest = hashlib.sha256(lease_bytes).hexdigest()
                        verified_lease = verify_test_only_lease_artifact(root, lease_bytes, lease_digest)
                        if verified_lease is not None and verified_lease.get("lease_reconciliation") == lease:
                            lease_name = f"{name}-test-only-lease-reconciliation.json"
                            (stage / lease_name).write_bytes(lease_bytes)
                            report["test_only_lease_artifact"] = {"path": lease_name,
                                "sha256": lease_digest, "source_sha256": digest,
                                "verified_against_rehydrated_archive": True}
                        else:
                            trace["eligible"] = False
                    else:
                        trace["eligible"] = False
                report["test_only_status"] = "TEST ONLY"
                report["test_only_permission_trace"] = {**trace,
                    "label": "TEST ONLY — does not establish real source clearance",
                    "report_status": "TEST ONLY", "production_source_clearance": False}
            report_path = f"{name}-report.json"
            _atomic_json(stage / report_path, report)
            text_lines = [f"Filing report: {name}", f"Status: {issuer_status}",
                f"Source SHA-256: {digest}", "Rights: unknown; no real source clearance",
                "First public availability: unknown", "No valuation claims; no workbook promotion."]
            text_lines.extend(f"{key}: {value.get('status')}" for key, value in reconciliations.items())
            if trace is not None:
                text_lines.append("TEST ONLY permission trace; this is not real source clearance.")
            text_path = stage / f"{name}-report.txt"
            text_path.write_text("\n".join(text_lines) + "\n", encoding="utf-8")
            reports[name] = {"report": report_path, "observations": observations_path,
                "report_txt": text_path.name, "source_sha256": digest,
                "report_sha256": hashlib.sha256((stage / report_path).read_bytes()).hexdigest(),
                "report_txt_sha256": hashlib.sha256(text_path.read_bytes()).hexdigest()}
            ledger.append({"issuer": name, **reports[name]})
        state = {"schema": "airline-filing-analysis-run/1", "status": "complete",
            "archive_manifest_sha256": manifest_digest, "issuers": reports,
            "workbook_promotion": False, "valuation_claims": False}
        _atomic_json(stage / "run-state.json", state)
        _atomic_json(stage / "ledger.json", {"schema": "airline-filing-analysis-ledger/1", "entries": ledger})
        os.replace(stage, output)
    except Exception:
        shutil.rmtree(stage, ignore_errors=True)
        raise
    return output
