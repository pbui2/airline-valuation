import copy
import hashlib
import json
import shutil

import pytest

from airline_valuation.filing_archive import import_filings
from airline_valuation.filing_registry import FILINGS
from airline_valuation.sec import _allowed_sec_url
from conftest import sec_fixture_files


ISSUERS = ("southwest", "delta", "american", "united")


def _inputs(tmp_path, names=ISSUERS):
    filenames = [
        FILINGS[name]["artifacts"][kind]["filename"]
        for name in names
        for kind in ("submissions", "index", "primary")
    ]
    files = sec_fixture_files(*filenames)
    metadata, documents = tmp_path / "metadata", tmp_path / "documents"
    metadata.mkdir(parents=True)
    documents.mkdir(parents=True)
    for name in names:
        for kind in ("submissions", "index"):
            filename = FILINGS[name]["artifacts"][kind]["filename"]
            shutil.copyfile(files[filename], metadata / filename)
        filename = FILINGS[name]["artifacts"]["primary"]["filename"]
        shutil.copyfile(files[filename], documents / filename)
    return metadata, documents


def _repin_test_inputs(tmp_path, monkeypatch, *, submissions=None, index=None):
    """Use copied pins to reach identity checks with controlled synthetic bytes."""
    import airline_valuation.filing_archive as filing_archive

    pins = copy.deepcopy(FILINGS)
    monkeypatch.setattr(filing_archive, "FILINGS", pins)
    metadata, documents = _inputs(tmp_path, ("american",))
    replacements = {"submissions": submissions, "index": index}
    for kind, raw in replacements.items():
        if raw is None:
            continue
        spec = pins["american"]["artifacts"][kind]
        spec["length"] = len(raw)
        spec["sha256"] = hashlib.sha256(raw).hexdigest()
        root = documents if kind == "primary" else metadata
        (root / spec["filename"]).write_bytes(raw)
    return metadata, documents


def _json_bytes(value):
    return json.dumps(value, separators=(",", ":")).encode()


def test_four_airline_registry_offline_import(tmp_path):
    metadata, documents = _inputs(tmp_path)
    archive = import_filings(metadata, documents, tmp_path / "archive")
    manifest = json.loads((archive / "manifest.json").read_text())
    assert set(manifest["filings"]) == set(ISSUERS)
    for filing in manifest["filings"].values():
        assert all(
            artifact["retrieval_time_status"] == "unknown for offline fixture import"
            for artifact in filing["artifacts"].values()
        )
    assert manifest["filings"]["american"]["primary_document"] == "aal-20251231.htm"
    assert manifest["filings"]["united"]["primary_document"] == "ual-20251231.htm"


def test_american_eight_k_and_amendment_rejection(tmp_path, monkeypatch):
    metadata, documents = _inputs(tmp_path, ("american",))
    with pytest.raises(ValueError, match="does not identify a registered original filing"):
        import_filings(
            metadata, documents, tmp_path / "wrong-accession",
            cik="0000006201", accession="0000006201-26-000008",
        )

    submissions = json.loads((metadata / FILINGS["american"]["artifacts"]["submissions"]["filename"]).read_text())
    recent = submissions["filings"]["recent"]
    index = recent["accessionNumber"].index(FILINGS["american"]["accession"])
    wrong_accession_index = recent["accessionNumber"].index("0000006201-26-000008")
    assert recent["form"][wrong_accession_index] == "8-K"

    wrong_form = copy.deepcopy(submissions)
    wrong_form["filings"]["recent"]["form"][index] = "8-K"
    repinned, documents = _repin_test_inputs(
        tmp_path / "wrong-form", monkeypatch, submissions=_json_bytes(wrong_form),
    )
    with pytest.raises(ValueError, match="form/report date/primary document mismatch"):
        import_filings(repinned, documents, tmp_path / "wrong-form-archive")
    assert not (tmp_path / "wrong-form-archive").exists()

    recent["form"][index] = "10-K/A"
    mutated, documents = _repin_test_inputs(
        tmp_path / "amended", monkeypatch, submissions=_json_bytes(submissions),
    )
    with pytest.raises(ValueError, match="form/report date/primary document mismatch"):
        import_filings(mutated, documents, tmp_path / "amended-archive")
    assert not (tmp_path / "amended-archive").exists()


def test_four_airline_archive_identity_matrix(tmp_path, monkeypatch):
    original_meta, _ = _inputs(tmp_path / "original", ("american",))
    source_submissions = json.loads(
        (original_meta / FILINGS["american"]["artifacts"]["submissions"]["filename"]).read_text()
    )
    source_index = json.loads(
        (original_meta / FILINGS["american"]["artifacts"]["index"]["filename"]).read_text()
    )
    recent = source_submissions["filings"]["recent"]
    item = recent["accessionNumber"].index(FILINGS["american"]["accession"])
    cases = []
    wrong_issuer = copy.deepcopy(source_submissions)
    wrong_issuer["cik"] = "100517"
    cases.append(("issuer", _json_bytes(wrong_issuer), None, "submissions CIK mismatch"))
    wrong_accession = copy.deepcopy(source_submissions)
    wrong_accession["filings"]["recent"]["accessionNumber"][item] = "0000006201-26-000008"
    cases.append(("accession", _json_bytes(wrong_accession), None, "registered accession not found"))
    wrong_index = copy.deepcopy(source_index)
    wrong_index["directory"]["name"] = "000000620126000008"
    cases.append(("index", None, _json_bytes(wrong_index), "index accession directory mismatch"))
    wrong_primary = copy.deepcopy(source_index)
    wrong_primary["directory"]["item"] = [
        entry for entry in wrong_primary["directory"]["item"]
        if entry.get("name") != FILINGS["american"]["primary_document"]
    ]
    cases.append(("primary", None, _json_bytes(wrong_primary), "primary document absent"))

    for label, submissions, index, message in cases:
        case_root = tmp_path / label
        metadata, documents = _repin_test_inputs(
            case_root, monkeypatch, submissions=submissions, index=index,
        )
        with pytest.raises(ValueError, match=message):
            import_filings(metadata, documents, case_root / "archive")
        assert not (case_root / "archive").exists()

    metadata, documents = _inputs(tmp_path / "symlink", ("american",))
    target = sec_fixture_files(FILINGS["american"]["artifacts"]["index"]["filename"])
    filename = FILINGS["american"]["artifacts"]["index"]["filename"]
    (metadata / filename).unlink()
    (metadata / filename).symlink_to(target[filename])
    with pytest.raises(ValueError, match="missing regular artifact"):
        import_filings(metadata, documents, tmp_path / "symlink-archive")


def test_new_fixture_hash_pins_reject_changed_bytes(tmp_path):
    metadata, documents = _inputs(tmp_path, ("american",))
    path = metadata / FILINGS["american"]["artifacts"]["index"]["filename"]
    path.write_bytes(path.read_bytes() + b" ")
    with pytest.raises(ValueError, match="length and SHA-256"):
        import_filings(metadata, documents, tmp_path / "archive")
    assert not (tmp_path / "archive").exists()


def test_registry_artifact_digests_are_well_formed():
    for issuer in ("american", "united"):
        assert set(FILINGS[issuer]["artifacts"]) == {"submissions", "index", "primary"}
        for artifact in FILINGS[issuer]["artifacts"].values():
            assert len(artifact["sha256"]) == 64
            assert all(c in "0123456789abcdef" for c in artifact["sha256"])
            assert artifact["length"] > 0


def test_sec_allowlist_accepts_only_exact_new_filing_urls():
    accepted = (
        "https://www.sec.gov/Archives/edgar/data/6201/000000620126000014/index.json",
        "https://www.sec.gov/Archives/edgar/data/6201/000000620126000014/aal-20251231.htm",
        "https://www.sec.gov/Archives/edgar/data/100517/000010051726000023/index.json",
        "https://www.sec.gov/Archives/edgar/data/100517/000010051726000023/ual-20251231.htm",
    )
    assert all(_allowed_sec_url(url) for url in accepted)
    assert not _allowed_sec_url(accepted[0].replace("000000620126000014", "000000620126000015"))
    assert not _allowed_sec_url(accepted[1] + "?download=1")
    assert not _allowed_sec_url(accepted[3].replace("ual-20251231.htm", "ual-20251231.htm5"))
