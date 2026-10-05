"""Offline-first, hash-pinned import of authorized local SEC response bodies."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from .filing_registry import FILINGS, REGISTRY_VERSION

MAX_RESPONSE_BYTES = 12 * 1024 * 1024


def _safe_file(root: Path, name: str) -> Path:
    if Path(name).name != name or name in {".", ".."} or "/" in name or "\\" in name:
        raise ValueError("unsafe artifact filename")
    root = root.absolute()
    for component in (root, *root.parents):
        if component.is_symlink():
            raise ValueError("symlink path component is not allowed")
    path = root / name
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"missing regular artifact file: {name}")
    return path


def _read(root: Path, spec: dict) -> bytes:
    path = _safe_file(root, spec["filename"])
    with path.open("rb") as stream:
        data = stream.read(MAX_RESPONSE_BYTES + 1)
    if len(data) > MAX_RESPONSE_BYTES:
        raise ValueError("artifact exceeds maximum response size")
    if len(data) != spec["length"] or hashlib.sha256(data).hexdigest() != spec["sha256"]:
        raise ValueError(f"artifact does not match code-owned length and SHA-256 pin: {spec['filename']}")
    return data


def _check_identity(issuer: dict, submissions_raw: bytes, index_raw: bytes) -> None:
    submissions = json.loads(submissions_raw)
    index = json.loads(index_raw)
    if str(submissions.get("cik", "")).zfill(10) != issuer["cik"]:
        raise ValueError("submissions CIK mismatch")
    recent = submissions.get("filings", {}).get("recent", {})
    accession = issuer["accession"]
    try:
        idx = recent["accessionNumber"].index(accession)
    except (KeyError, ValueError, AttributeError):
        raise ValueError("registered accession not found in recent submissions") from None
    expected = {"form": issuer["form"], "reportDate": issuer["report_date"], "primaryDocument": issuer["primary_document"]}
    if any(recent.get(key, [])[idx] != value for key, value in expected.items()):
        raise ValueError("submissions form/report date/primary document mismatch")
    directory = index.get("directory", {}).get("name")
    if not isinstance(directory, str) or directory.rstrip("/").rsplit("/", 1)[-1] != accession.replace("-", ""):
        raise ValueError("index accession directory mismatch")
    items = index.get("directory", {}).get("item", [])
    if not any(item.get("name") == issuer["primary_document"] for item in items if isinstance(item, dict)):
        raise ValueError("registered primary document absent from filing index")


def import_filings(metadata_dir: Path, document_dir: Path, output_dir: Path, *, retrieved_at: datetime | None = None, cik: str | None = None, accession: str | None = None, provenance_path: Path | None = None) -> Path:
    """Validate exact pinned response bodies and atomically create archive directory.

    Input names are the six fixed code-owned names in filing_registry. Metadata
    contains independently supplied response URLs, response headers, and UTC
    retrieval time; this importer does not infer historic availability from time.
    """
    metadata_dir, document_dir, output_dir = map(Path, (metadata_dir, document_dir, output_dir))
    if (cik is None) != (accession is None):
        raise ValueError("--cik and --accession must be supplied together")
    if cik is not None:
        matches = [name for name, issuer in FILINGS.items() if issuer["cik"].lstrip("0") == str(cik).lstrip("0") and issuer["accession"] == accession]
        if len(matches) != 1:
            raise ValueError("CIK/accession does not identify a registered original filing")
        selected = matches
    else:
        selected = []
        for name, issuer in FILINGS.items():
            specs = issuer["artifacts"]
            present = [((document_dir if kind == "primary" else metadata_dir) / spec["filename"]).exists() or ((document_dir if kind == "primary" else metadata_dir) / spec["filename"]).is_symlink() for kind, spec in specs.items()]
            if any(present):
                selected.append(name)
        if not selected:
            raise ValueError("no registered filing artifacts found in supplied directories")
    for root in (metadata_dir, document_dir, output_dir.parent):
        absolute = root.absolute()
        if any(part.is_symlink() for part in (absolute, *absolute.parents)):
            raise ValueError("symlink path component is not allowed")
    timestamp = retrieved_at or datetime.now(timezone.utc)
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise ValueError("retrieval timestamp must be timezone-aware")
    if output_dir.exists() or output_dir.is_symlink():
        raise FileExistsError(f"output directory already exists: {output_dir}")
    validated = {}
    for name in selected:
        issuer = FILINGS[name]
        group = {}
        for kind, spec in issuer["artifacts"].items():
            root = document_dir if kind == "primary" else metadata_dir
            raw = _read(root, spec)
            group[kind] = raw
        _check_identity(issuer, group["submissions"], group["index"])
        validated[name] = group
    acquired_provenance = {}
    if provenance_path is not None:
        provenance_path = Path(provenance_path)
        _safe_file(provenance_path.parent, provenance_path.name)
        try:
            raw_provenance = json.loads(provenance_path.read_bytes())
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"invalid acquisition provenance: {exc}") from None
        if raw_provenance.get("schema") != "airline-sec-acquisition/1":
            raise ValueError("unrecognized acquisition provenance schema")
        for name in selected:
            issuer = FILINGS[name]
            record = raw_provenance.get("filing", {})
            if record.get("cik") != issuer["cik"] or record.get("accession") != issuer["accession"]:
                raise ValueError("acquisition provenance filing identity mismatch")
            artifacts = raw_provenance.get("artifacts", {})
            for kind, spec in issuer["artifacts"].items():
                item = artifacts.get(kind)
                body = validated[name][kind]
                if not isinstance(item, dict) or item.get("artifact_url") != spec["url"] or item.get("actual_response_url") != spec["url"]:
                    raise ValueError("acquisition provenance URL does not match registered SEC resource")
                parsed = urlparse(item["actual_response_url"])
                if parsed.scheme != "https" or parsed.hostname not in {"data.sec.gov", "www.sec.gov"} or parsed.username or parsed.password or parsed.query or parsed.fragment:
                    raise ValueError("acquisition provenance actual URL is not an allowed SEC URL")
                if item.get("byte_length") != len(body) or item.get("sha256") != hashlib.sha256(body).hexdigest():
                    raise ValueError("acquisition provenance byte length or hash mismatch")
                try:
                    retrieved = datetime.fromisoformat(item["retrieved_at"])
                    if retrieved.tzinfo is None or retrieved.utcoffset() is None:
                        raise ValueError
                except (TypeError, ValueError):
                    raise ValueError("acquisition provenance retrieval time is invalid") from None
                acquired_provenance[(name, kind)] = item
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=f".{output_dir.name}.stage-", dir=output_dir.parent))
    try:
        manifest = {"schema": REGISTRY_VERSION, "rights": "unknown; provenance does not grant redistribution rights", "archive_created_at": timestamp.astimezone(timezone.utc).isoformat(), "filings": {}}
        for name in selected:
            issuer = FILINGS[name]
            filing = {k: issuer[k] for k in ("cik", "accession", "form", "report_date", "primary_document")}
            filing["artifacts"] = {}
            for kind, spec in issuer["artifacts"].items():
                raw = validated[name][kind]
                filename = {"submissions": "submissions.json", "index": "index.json", "primary": issuer["primary_document"]}[kind]
                (stage / f"{name}-{filename}").write_bytes(raw)
                provenance_item = acquired_provenance.get((name, kind))
                filing["artifacts"][kind] = {"path": f"{name}-{filename}", "artifact_url": spec["url"], "actual_response_url": provenance_item["actual_response_url"] if provenance_item else None, "content_type": provenance_item.get("content_type") if provenance_item else spec["content_type"], "byte_length": len(raw), "sha256": hashlib.sha256(raw).hexdigest(), "retrieved_at": provenance_item["retrieved_at"] if provenance_item else None, "retrieval_time_status": "validated acquisition provenance" if provenance_item else "unknown for offline fixture import"}
            manifest["filings"][name] = filing
        (stage / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        for path in stage.iterdir():
            if path.is_symlink() or not path.is_file():
                raise ValueError("unsafe staged archive entry")
        os.replace(stage, output_dir)
    except Exception:
        shutil.rmtree(stage, ignore_errors=True)
        raise
    return output_dir
