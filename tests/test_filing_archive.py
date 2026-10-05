import json
import shutil
from datetime import datetime, timezone
from pathlib import Path

import pytest

from airline_valuation.filing_archive import import_filings
from airline_valuation.filing_registry import FILINGS
from airline_valuation import cli
from airline_valuation.sec import SECAcquisition
from conftest import sec_fixture_files


def _inputs(tmp_path, names=("southwest", "delta")):
    filenames = [
        FILINGS[name]["artifacts"][kind]["filename"]
        for name in names
        for kind in ("submissions", "index", "primary")
    ]
    files = sec_fixture_files(*filenames)
    meta, docs = tmp_path / "metadata", tmp_path / "documents"
    meta.mkdir(); docs.mkdir()
    for name in names:
        for kind in ("submissions", "index"):
            filename = FILINGS[name]["artifacts"][kind]["filename"]
            shutil.copyfile(files[filename], meta / filename)
        filename = FILINGS[name]["artifacts"]["primary"]["filename"]
        shutil.copyfile(files[filename], docs / filename)
    return meta, docs


def test_filing_archive_two_issuers(tmp_path):
    meta, docs = _inputs(tmp_path)
    out = import_filings(meta, docs, tmp_path / "archive")
    manifest = json.loads((out / "manifest.json").read_text())
    assert set(manifest["filings"]) == {"southwest", "delta"}
    assert manifest["filings"]["delta"]["primary_document"] == "dal-20251231.htm"
    assert manifest["rights"].startswith("unknown")


def test_filing_archive_offline_single_issuer(tmp_path):
    meta, docs = _inputs(tmp_path, ("southwest",))
    out = import_filings(meta, docs, tmp_path / "archive")
    manifest = json.loads((out / "manifest.json").read_text())
    assert set(manifest["filings"]) == {"southwest"}
    artifact = manifest["filings"]["southwest"]["artifacts"]["primary"]
    assert artifact["retrieved_at"] is None
    assert artifact["actual_response_url"] is None
    assert artifact["retrieval_time_status"] == "unknown for offline fixture import"


def test_filing_archive_identity_and_amendment_isolation(tmp_path):
    meta, docs = _inputs(tmp_path, ("southwest",))
    (meta / "southwest_submissions.json").write_bytes(b"{}")
    with pytest.raises(ValueError, match="length and SHA-256"):
        import_filings(meta, docs, tmp_path / "archive")
    assert not (tmp_path / "archive").exists()


def test_filing_archive_atomic_rollback(tmp_path):
    meta, docs = _inputs(tmp_path)
    (docs / "delta_primary.html").write_bytes(b"stale")
    output = tmp_path / "archive"
    with pytest.raises(ValueError, match="length and SHA-256"):
        import_filings(meta, docs, output)
    assert not output.exists()
    assert not list(tmp_path.glob(".archive.stage-*"))


def test_rejects_symlink_input(tmp_path):
    meta, docs = _inputs(tmp_path, ("southwest",))
    (meta / "southwest_index.json").unlink()
    source = sec_fixture_files(FILINGS["southwest"]["artifacts"]["index"]["filename"])
    (meta / "southwest_index.json").symlink_to(source["southwest_index.json"])
    with pytest.raises(ValueError, match="regular artifact"):
        import_filings(meta, docs, tmp_path / "archive")


def test_public_cli_acquire_then_import_one_issuer(tmp_path, monkeypatch, capsys):
    issuer = FILINGS["southwest"]
    files = sec_fixture_files(*(spec["filename"] for spec in issuer["artifacts"].values()))
    bodies = {kind: files[spec["filename"]].read_bytes() for kind, spec in issuer["artifacts"].items()}
    class InjectedClient:
        def __init__(self, user_agent):
            self.user_agent = user_agent
        def acquire(self, url):
            kind = next(k for k, v in issuer["artifacts"].items() if v["url"] == url)
            return SECAcquisition(url, bodies[kind], datetime(2026, 2, 7, tzinfo=timezone.utc), __import__("hashlib").sha256(bodies[kind]).hexdigest(), url, issuer["artifacts"][kind]["content_type"])
    monkeypatch.setattr(cli, "SECFilingClient", InjectedClient)
    acquisition = tmp_path / "acquired"
    assert cli.main(["filing-acquire", "--cik", issuer["cik"], "--accession", issuer["accession"], "--output-dir", str(acquisition), "--user-agent", "private-contact@example.com"]) == 0
    assert "private-contact@example.com" not in capsys.readouterr().out
    archive = tmp_path / "archive"
    assert cli.main(["filing-import", "--metadata-dir", str(acquisition / "metadata"), "--document-dir", str(acquisition / "documents"), "--output-dir", str(archive)]) == 0
    assert set(json.loads((archive / "manifest.json").read_text())["filings"]) == {"southwest"}
    artifact = json.loads((archive / "manifest.json").read_text())["filings"]["southwest"]["artifacts"]["primary"]
    assert artifact["actual_response_url"] == issuer["artifacts"]["primary"]["url"]
    assert artifact["retrieved_at"] == "2026-02-07T00:00:00+00:00"
