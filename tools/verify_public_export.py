#!/usr/bin/env python3
"""Standalone public-export verification script (AC.1-6).

This script is deliberately NOT collected by pytest.  It checks the six
export acceptance criteria against the private source baselines recorded in
the plan session directory and against this public repository's tree and
(once committed) Git history.

Usage:
    python3 tools/verify_public_export.py [--baseline-dir DIR] [--private-root DIR] [--no-git]

Checks implemented here:

* AC.1 test_source_preserved        - private source HEAD, full git status,
                                      tracked path set, and file hashes still
                                      match the recorded baselines (read-only).
* AC.2 test_public_tree_exclusions  - working tree contains no excluded
                                      workbook/private content: path checks
                                      plus binary/credential/personal-email
                                      and sizeable workbook/comment-excerpt
                                      content heuristics.
* AC.2 test_public_git_history_exclusions - no xlsx, review artifacts, policy
                                      drafts, source .git objects, private
                                      credentials/emails, or sizeable
                                      workbook/comment excerpts in committed
                                      paths or reachable blobs.
* AC.3 test_public_readme_contract  - README credits Damodaran, links the
                                      original, and states the owner keeps the
                                      fully functional version (no misleading
                                      full-functionality claim).
* AC.4 test_public_checkout_suite   - a clean hermetic checkout runs the
                                      portable pytest command (with -rs) with
                                      substantive passes and explicit
                                      private-input skip reasons.
* AC.5 test_strict_mode_fails_hermetic / test_synthetic_workbook_suite -
                                      strict mode fails clearly in a hermetic
                                      checkout whose private-input roots are
                                      absent, and synthetic parser/writer
                                      invariants pass.
* AC.6 test_public_git_isolation    - single parentless root commit authored
                                      AND committed by the public identity,
                                      exactly one branch ref, no remote,
                                      clean tree.

Exit code 0 when all applicable checks pass; 1 otherwise.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

PUBLIC_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PRIVATE_ROOT = PUBLIC_ROOT.parent / "airline_valuation_scaffold_v4"
DEFAULT_BASELINE_DIR = Path("/home/dolby/.local/share/polytoken/sessions-v1/0bxywh-coral")

# Exact identity (name + email) the public root commit must carry for both the
# author and the committer fields.
PUBLIC_AUTHOR = "pbui2 <337151103+pbui2@users.noreply.github.com>"
PUBLIC_EMAIL_BYTES = b"337151103+pbui2@users.noreply.github.com"

# Paths that must never appear in the public export.
EXCLUDED_REL_PATHS = [
    "review-artifacts",
    "docs/policy-drafts",
    "docs/provisional-comment-review.md",
    "docs/provisional-input-scenario.md",
    "docs/evidence-reconciliation-2025.md",
    "docs/ginzu-input-policy-v1.md",
    "docs/ginzu-input-policy-v2.md",
    "docs/southwest-input-review.md",
    "docs/southwest-valuation-decision-packet.md",
    "docs/workbook-map.md",
    "tests/fixtures",
    "private-review",
    "fcffsimpleginzu.xlsx",
]

PRIVATE_DOC_FILENAMES = {
    "provisional-comment-review.md",
    "provisional-input-scenario.md",
    "evidence-reconciliation-2025.md",
    "ginzu-input-policy-v1.md",
    "ginzu-input-policy-v2.md",
    "southwest-input-review.md",
    "southwest-valuation-decision-packet.md",
    "workbook-map.md",
    "ginzu_draft_scenarios_v1.json",
    "fy2025-manifest.json",
    "fy2025-provisional-scenario.json",
}

# Path-level credential filename signal.
CREDENTIAL_FILENAME_RE = re.compile(r"(?i)\.pem$|\.key$|credentials|secrets|password")

# Content-level credential/secret signals (defensible, fixed-pattern heuristics).
PRIVATE_KEY_RE = re.compile(rb"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----")
AWS_ACCESS_KEY_RE = re.compile(rb"AKIA[0-9A-Z]{16}")
OPENAI_API_KEY_RE = re.compile(rb"sk-[A-Za-z0-9]{20,}")

# Personal-email signal.  The public identity and RFC-2606 reserved example
# domains (used by tests as placeholders) are expected and explicitly allowed.
EMAIL_RE = re.compile(rb"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
ALLOWED_EMAIL_DOMAINS = (b"example.com", b"example.org", b"example.net", b"example.edu")

# Binary/archive magic bytes checked at the start of file/blob content.
BINARY_SIGNATURES = [
    ("zip/xlsx archive", b"PK\x03\x04"),
    ("OLE2 legacy xls", b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"),
    ("gzip archive", b"\x1f\x8b"),
    ("PDF", b"%PDF"),
    ("ELF binary", b"\x7fELF"),
    ("PE executable", b"MZ"),
]

# Sizeable workbook/comment-excerpt heuristics.  Real worksheet parts contain
# many cell/row elements and real comment parts contain many threadedComment
# elements.  The thresholds sit well above every occurrence measured in the
# current legitimate tree (max 15 cell refs / 7 rows / 3 comment tags), so
# pinned contract fragments and test fixtures do not false-positive while a
# genuine sizeable excerpt would.
WORKBOOK_EXCERPT_MIN_BYTES = 4096
COMMENT_EXCERPT_MIN_BYTES = 2048
WORKBOOK_CELL_MIN = 25
WORKBOOK_ROW_MIN = 20
COMMENT_TAG_MIN = 8
CELL_REF_RE = re.compile(rb'<c r="[A-Z]+\d+"')
ROW_REF_RE = re.compile(rb'<row r="[0-9]+"')
COMMENT_TAG_RE = re.compile(rb"<threadedComment\b")


def run(cmd, cwd=None, env=None, check=False):
    return subprocess.run(
        cmd, cwd=cwd, env=env, capture_output=True, text=True, check=check
    )


def run_bytes(cmd, cwd=None, env=None):
    return subprocess.run(cmd, cwd=cwd, env=env, capture_output=True)


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def parse_hash_lines(text: str) -> dict[str, str]:
    """Parse '<sha256>  <path>' lines into {path: sha256}."""
    mapping = {}
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        digest, _, path = line.partition("  ")
        if not path:
            digest, path = line.split(None, 1)
        mapping[path] = digest
    return mapping


def _git_committed(root: Path) -> bool:
    """True when the repo has at least one commit (a real history exists)."""
    if not (root / ".git").is_dir():
        return False
    proc = run(["git", "-C", str(root), "rev-list", "--count", "--all"])
    return proc.returncode == 0 and proc.stdout.strip() not in ("", "0")


def _copy_public_tree(src: Path, dst: Path) -> None:
    """Copy the public tree for hermetic runs, dropping toolchain noise."""
    shutil.copytree(
        src, dst,
        ignore=shutil.ignore_patterns(".venv", "__pycache__", ".git",
                                      ".pytest_cache", "*.pyc"),
    )


# ---------------------------------------------------------------------------
# AC.2 shared path/content heuristics
# ---------------------------------------------------------------------------


def _path_violations(rel: str) -> list[str]:
    """Path-level exclusion checks shared by the tree and history scans."""
    violations = []
    lowered = rel.lower()
    if lowered.endswith((".xlsx", ".xlsm", ".xlsb", ".xls")):
        violations.append(f"workbook path: {rel}")
    if Path(rel).name in PRIVATE_DOC_FILENAMES:
        violations.append(f"private doc path: {rel}")
    if CREDENTIAL_FILENAME_RE.search(Path(rel).name):
        violations.append(f"possible credential filename: {rel}")
    for excluded in EXCLUDED_REL_PATHS:
        if rel == excluded or rel.startswith(excluded + "/"):
            violations.append(f"excluded path: {rel}")
    return violations


def _content_violations(content: bytes, where: str) -> list[str]:
    """Content-level heuristics: binary/archive signatures, credential and
    personal-email patterns, and sizeable workbook/comment excerpts.

    These are best-effort, defensible signals.  They detect known magic bytes
    and fixed patterns only; renamed, split, encrypted, or compressed payloads
    can evade them.
    """
    violations = []
    head = content[:16]
    for label, magic in BINARY_SIGNATURES:
        if head.startswith(magic):
            violations.append(f"binary/archive content ({label}): {where}")
            break
    if PRIVATE_KEY_RE.search(content):
        violations.append(f"embedded private key material: {where}")
    if AWS_ACCESS_KEY_RE.search(content):
        violations.append(f"possible AWS access key: {where}")
    if OPENAI_API_KEY_RE.search(content):
        violations.append(f"possible API key (sk-...): {where}")
    for email in EMAIL_RE.findall(content):
        if email == PUBLIC_EMAIL_BYTES:
            continue
        domain = email.rsplit(b"@", 1)[-1].lower()
        if domain in ALLOWED_EMAIL_DOMAINS:
            continue
        if any(domain.endswith(b"." + suffix) for suffix in ALLOWED_EMAIL_DOMAINS):
            continue
        violations.append(f"personal email in content: {where} ({email.decode('ascii', 'replace')})")
        break
    if len(content) >= WORKBOOK_EXCERPT_MIN_BYTES:
        cells = len(CELL_REF_RE.findall(content))
        rows = len(ROW_REF_RE.findall(content))
        if cells >= WORKBOOK_CELL_MIN or rows >= WORKBOOK_ROW_MIN:
            violations.append(
                f"sizeable workbook excerpt (cells={cells}, rows={rows}): {where}"
            )
    if len(content) >= COMMENT_EXCERPT_MIN_BYTES:
        tags = len(COMMENT_TAG_RE.findall(content))
        if tags >= COMMENT_TAG_MIN:
            violations.append(
                f"sizeable threaded-comment excerpt ({tags} comment tags): {where}"
            )
    return violations


AC2_LIMITATION_NOTE = (
    "limitations: heuristics detect known binary magic bytes and fixed "
    "credential/email/cell patterns only; renamed, split, encrypted, or "
    "compressed payloads and semantically obfuscated content can evade them"
)


# ---------------------------------------------------------------------------
# AC.1
# ---------------------------------------------------------------------------


def test_source_preserved(private_root: Path, baseline_dir: Path) -> tuple[bool, str]:
    head_file = baseline_dir / "source-head.txt"
    tracked_file = baseline_dir / "source-tracked.sha256"
    status_file = baseline_dir / "source-status.txt"
    untracked_paths_file = baseline_dir / "source-untracked-paths.txt"
    untracked_sha_file = baseline_dir / "source-untracked.sha256"
    required = [
        head_file, tracked_file, status_file, untracked_paths_file, untracked_sha_file,
    ]
    for path in required:
        if not path.is_file():
            return False, f"missing baseline file {path.name}"

    head = run(["git", "rev-parse", "HEAD"], cwd=private_root)
    if head.returncode != 0 or head.stdout.strip() != head_file.read_text().strip():
        return False, f"private HEAD changed: got {head.stdout.strip()!r}"

    # Compare the whole porcelain status (tracked modifications, staged
    # changes, untracked files) line-for-line, not just the untracked subset.
    status = run(["git", "status", "--porcelain", "--untracked-files=all"], cwd=private_root)
    baseline_status = status_file.read_text().strip().splitlines()
    if status.returncode != 0:
        return False, "git status failed in private source"
    current_status = status.stdout.strip().splitlines()
    if current_status != baseline_status:
        return False, (
            "private git status differs from baseline: "
            f"current={current_status} baseline={baseline_status}"
        )

    # Compare the tracked path set (git ls-files) as well as hashes, so an
    # added/removed/renamed tracked file is caught even when the hashes of the
    # remaining files are unchanged.
    tracked = run(["git", "ls-files"], cwd=private_root)
    if tracked.returncode != 0:
        return False, "git ls-files failed in private source"
    tracked_expected = parse_hash_lines(tracked_file.read_text())
    current_paths = sorted(tracked.stdout.splitlines())
    expected_paths = sorted(tracked_expected)
    if current_paths != expected_paths:
        return False, (
            "private tracked path set changed: "
            f"current={current_paths} baseline={expected_paths}"
        )

    for rel, digest in tracked_expected.items():
        path = private_root / rel
        if not path.is_file():
            return False, f"tracked file missing: {rel}"
        if sha256_file(path) != digest:
            return False, f"tracked file hash changed: {rel}"

    untracked_paths = untracked_paths_file.read_text().splitlines()
    untracked_expected = parse_hash_lines(untracked_sha_file.read_text())
    for rel in untracked_paths:
        path = private_root / rel
        if not path.is_file():
            return False, f"untracked artifact missing: {rel}"
        if sha256_file(path) != untracked_expected.get(rel, ""):
            return False, f"untracked artifact hash changed: {rel}"

    return True, "private source HEAD, full status, tracked path set, and hashes match recorded baselines"


# ---------------------------------------------------------------------------
# AC.2
# ---------------------------------------------------------------------------


def _walk_public(root: Path):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in {".git", "__pycache__", ".venv"}]
        for name in filenames:
            yield Path(dirpath) / name


def test_public_tree_exclusions(root: Path) -> tuple[bool, str]:
    violations = []
    for path in _walk_public(root):
        rel = path.relative_to(root).as_posix()
        violations.extend(_path_violations(rel))
        try:
            content = path.read_bytes()
        except OSError:
            continue
        violations.extend(_content_violations(content, rel))
    if violations:
        return False, "; ".join(violations[:20])
    return True, f"working tree contains no excluded workbook/private content ({AC2_LIMITATION_NOTE})"


def test_public_git_history_exclusions(root: Path) -> tuple[bool, str]:
    if not (root / ".git").is_dir():
        return False, "SKIP - no git repository yet"
    git = ["git", "-C", str(root)]
    listed = run(git + ["rev-list", "--objects", "--all"])
    if listed.returncode != 0:
        return False, "git rev-list failed"

    violations: list[str] = []
    path_by_hash: dict[str, str] = {}
    for line in listed.stdout.splitlines():
        parts = line.split(" ", 1)
        if len(parts) == 2:
            path_by_hash.setdefault(parts[0], parts[1])
            for violation in _path_violations(parts[1]):
                violations.append(f"{violation} (history)")

    # Determine which objects are blobs with a single batched cat-file call.
    batch = subprocess.run(
        git + ["cat-file", "--batch-check=%(objectname) %(objecttype)"],
        input=("\n".join(path_by_hash) + "\n").encode(),
        capture_output=True,
        cwd=str(root),
    )
    blob_hashes: list[str] = []
    if batch.returncode == 0:
        for line in batch.stdout.splitlines():
            parts = line.split()
            if len(parts) == 2 and parts[1] == b"blob":
                blob_hashes.append(parts[0].decode())

    scanned = 0
    for blob_hash in dict.fromkeys(blob_hashes):
        blob = run_bytes(git + ["cat-file", "blob", blob_hash])
        if blob.returncode != 0:
            continue
        scanned += 1
        where = path_by_hash.get(blob_hash, f"blob:{blob_hash[:8]}")
        for violation in _content_violations(blob.stdout, where):
            violations.append(f"{violation} (history)")

    if violations:
        return False, "; ".join(violations[:20])
    return True, (
        f"committed paths and {scanned} reachable blobs contain no excluded "
        f"content ({AC2_LIMITATION_NOTE})"
    )


# ---------------------------------------------------------------------------
# AC.3
# ---------------------------------------------------------------------------


def test_public_readme_contract(root: Path) -> tuple[bool, str]:
    readme = root / "README.md"
    if not readme.is_file():
        return False, "README.md missing"
    text = readme.read_text(encoding="utf-8")
    required = [
        "Damodaran",
        "https://pages.stern.nyu.edu/~adamodar/New_Home_Page/spreadsh.htm",
        "https://pages.stern.nyu.edu/~adamodar/pc/fcffsimpleginzu.xlsx",
    ]
    for token in required:
        if token not in text:
            return False, f"README missing token: {token}"
    for phrase in ("fully functional", "owner", "rehosting", "completed valuation"):
        if phrase not in text:
            return False, f"README missing phrase: {phrase}"
    if "include his template" not in text and "not include" not in text:
        return False, "README does not state the template is not redistributed"
    return True, "README credits Damodaran and states the private boundary"


# ---------------------------------------------------------------------------
# AC.5
# ---------------------------------------------------------------------------


def _hermetic_env(root: Path, home_dir: Path, fixture_root: Path) -> dict:
    """Build a hermetic child environment.

    The base environment is minimal: it inherits nothing from the caller except
    PATH, so inherited ``AIRLINE_VALUATION_*`` overrides (the owner's SEC
    fixture locator and any strict flags) are discarded by construction.  The
    explicit hermetic SEC/private locators and cleared strict flags are then
    set, so they always win.
    """
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(home_dir),
        "PYTHONPATH": str(root / "src"),
    }
    env["AIRLINE_VALUATION_SEC_FIXTURE_DIR"] = str(fixture_root / "sec")
    env["AIRLINE_VALUATION_REQUIRE_SEC_FIXTURES"] = ""
    env["AIRLINE_VALUATION_REQUIRE_PRIVATE_INPUTS"] = ""
    return env


def test_strict_mode_fails_hermetic(root: Path, python: str) -> tuple[bool, str]:
    node = "tests/test_workbook.py::test_real_contract_and_explicit_input_allowlist"
    with tempfile.TemporaryDirectory(prefix="public-strict-") as tmp:
        tmp_root = Path(tmp) / "checkout"
        _copy_public_tree(root, tmp_root)
        home_dir = Path(tmp) / "home"
        home_dir.mkdir()
        fixture_root = Path(tmp) / "fixtures"
        fixture_root.mkdir()
        env = _hermetic_env(tmp_root, home_dir, fixture_root)
        env["AIRLINE_VALUATION_REQUIRE_PRIVATE_INPUTS"] = "1"
        proc = run(
            [python, "-m", "pytest", "-q", "-p", "no:cacheprovider", node],
            cwd=tmp_root, env=env,
        )
        output = proc.stdout + proc.stderr
        if proc.returncode == 0:
            return False, "strict mode did not fail on missing private workbook"
        if "requires private" not in output:
            return False, "strict-mode failure lacks explicit private-input reason"
        return True, "strict mode fails clearly on missing private workbook in a hermetic checkout"


def test_synthetic_workbook_suite(root: Path, python: str) -> tuple[bool, str]:
    with tempfile.TemporaryDirectory(prefix="public-synthetic-") as tmp:
        tmp_root = Path(tmp) / "checkout"
        _copy_public_tree(root, tmp_root)
        home_dir = Path(tmp) / "home"
        home_dir.mkdir()
        fixture_root = Path(tmp) / "fixtures"
        fixture_root.mkdir()
        env = _hermetic_env(tmp_root, home_dir, fixture_root)
        proc = run(
            [python, "-m", "pytest", "-q", "-p", "no:cacheprovider",
             "tests/test_public_synthetic_workbook.py", "tests/test_public_export_contract.py"],
            cwd=tmp_root, env=env,
        )
        if proc.returncode != 0:
            return False, f"synthetic/contract suite failed in hermetic checkout:\n{(proc.stdout + proc.stderr)[-1500:]}"
        if "passed" not in proc.stdout:
            return False, "no passing tests reported in synthetic suite"
        return True, "synthetic workbook invariants and export-contract tests pass in a hermetic checkout"


# ---------------------------------------------------------------------------
# AC.4
# ---------------------------------------------------------------------------


def _parse_summary_count(text: str, label: str) -> int:
    match = re.search(rf"(\d+)\s+{label}", text)
    return int(match.group(1)) if match else 0


def test_public_checkout_suite(root: Path, python: str, no_git: bool) -> tuple[bool, str]:
    if no_git or not _git_committed(root):
        return False, "SKIP - requires a committed git history (run after the root commit)"

    with tempfile.TemporaryDirectory(prefix="public-checkout-") as tmp:
        tmp_root = Path(tmp) / "checkout"
        proc = run(["git", "clone", "-q", str(root), str(tmp_root)])
        if proc.returncode != 0:
            return False, f"git clone failed: {proc.stderr.strip()}"

        home_dir = Path(tmp) / "home"
        home_dir.mkdir()
        fixture_root = Path(tmp) / "fixtures"
        fixture_root.mkdir()
        env = _hermetic_env(tmp_root, home_dir, fixture_root)

        suite = run(
            [python, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-rs"],
            cwd=tmp_root, env=env,
        )
        output = suite.stdout + suite.stderr
        if suite.returncode != 0:
            return False, f"clean-checkout suite failed:\n{output[-2000:]}"
        if "import" in output.lower() and "error" in output.lower():
            return False, "clean-checkout suite reported an import error"

        passed = _parse_summary_count(output, "passed")
        skipped = _parse_summary_count(output, "skipped")
        if not passed:
            return False, f"clean-checkout suite reported no substantive passes:\n{output[-1000:]}"
        if not skipped:
            return False, f"clean-checkout suite reported no private-input skips:\n{output[-1000:]}"
        for expected in ("requires private", "source workbook", "policy-draft", "review docs"):
            if expected not in output:
                return False, (
                    f"clean-checkout skip reasons lack explicit missing-input reason {expected!r}:\n"
                    f"{output[-1500:]}"
                )
        return True, (
            f"clean checkout suite passed ({passed} passed, {skipped} skipped) "
            "with explicit workbook/policy/review skip reasons"
        )


# ---------------------------------------------------------------------------
# AC.6
# ---------------------------------------------------------------------------


def test_public_git_isolation(root: Path, no_git: bool) -> tuple[bool, str]:
    if no_git:
        return False, "SKIP - --no-git requested"
    if not (root / ".git").is_dir():
        return False, "SKIP - no git repository yet"
    git = ["git", "-C", str(root)]

    refs = run(git + ["for-each-ref", "--format=%(refname)"]).stdout.splitlines()
    if len(refs) != 1 or not refs[0].startswith("refs/heads/"):
        return False, f"expected exactly one branch ref, found: {refs}"

    remotes = run(git + ["remote", "-v"])
    if remotes.stdout.strip():
        return False, f"remote configured: {remotes.stdout.strip()}"

    status = run(git + ["status", "--porcelain", "--untracked-files=all"])
    if status.stdout.strip():
        return False, f"working tree is not clean: {status.stdout.strip()[:200]}"

    commits = run(git + ["rev-list", "--all"])
    all_commits = commits.stdout.splitlines()
    if len(all_commits) != 1:
        return False, f"expected exactly one root commit, found {len(all_commits)}"
    commit = all_commits[0]

    parents = run(git + ["rev-list", "--parents", "-n", "1", commit])
    if parents.stdout.split()[1:]:
        return False, "root commit has a parent (not a fresh history)"

    ident = run(git + ["log", "-1", "--format=%an <%ae>|%cn <%ce>", commit]).stdout.strip()
    author, _, committer = ident.partition("|")
    if author != PUBLIC_AUTHOR:
        return False, f"commit author is not the public identity: {author!r}"
    if committer != PUBLIC_AUTHOR:
        return False, f"commit committer is not the public identity: {committer!r}"

    return True, "single parentless root commit, exact author+committer identity, one branch ref, no remote, clean tree"


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def results(checks, strict_skips: bool = False) -> list[str]:
    """Print check outcomes and return the failed-check names.

    A check reported as SKIP is tolerated only before a real commit exists
    (or when --no-git is requested).  Once a committed history is present,
    an unexpectedly skipped required check is treated as a failure.
    """
    failed = []
    for name, ok, detail in checks:
        skipped = detail.startswith("SKIP")
        if ok:
            status = "PASS"
        elif skipped and not strict_skips:
            status = "SKIP"
        else:
            status = "FAIL"
            if skipped and strict_skips:
                detail = f"{detail} (unexpected skip after commit)"
            failed.append(name)
        print(f"[{status}] {name}: {detail}")
    return failed


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify public export AC.1-6")
    parser.add_argument("--baseline-dir", default=str(DEFAULT_BASELINE_DIR), help="dir with source-*.txt/sha256 baselines")
    parser.add_argument("--private-root", default=str(DEFAULT_PRIVATE_ROOT), help="private source repository root (read-only)")
    parser.add_argument("--no-git", action="store_true", help="skip checks that require a committed git history")
    args = parser.parse_args()

    private_root = Path(args.private_root)
    baseline_dir = Path(args.baseline_dir)
    python = str(PUBLIC_ROOT / ".venv" / "bin" / "python")

    # After a real commit exists (and --no-git is not given), required checks
    # may no longer skip; unexpected skips become failures.
    strict_skips = _git_committed(PUBLIC_ROOT) and not args.no_git

    checks = []
    checks.append(("AC.1 test_source_preserved", *test_source_preserved(private_root, baseline_dir)))
    checks.append(("AC.2 test_public_tree_exclusions (working tree)", *test_public_tree_exclusions(PUBLIC_ROOT)))
    checks.append(("AC.2 test_public_git_history_exclusions (committed paths + reachable blobs)", *test_public_git_history_exclusions(PUBLIC_ROOT)))
    checks.append(("AC.3 test_public_readme_contract", *test_public_readme_contract(PUBLIC_ROOT)))
    checks.append(("AC.5 test_strict_mode_fails_hermetic", *test_strict_mode_fails_hermetic(PUBLIC_ROOT, python)))
    checks.append(("AC.5 test_synthetic_workbook_suite", *test_synthetic_workbook_suite(PUBLIC_ROOT, python)))
    checks.append(("AC.4 test_public_checkout_suite", *test_public_checkout_suite(PUBLIC_ROOT, python, args.no_git)))
    checks.append(("AC.6 test_public_git_isolation", *test_public_git_isolation(PUBLIC_ROOT, args.no_git)))

    failed = results(checks, strict_skips=strict_skips)
    print()
    if failed:
        print(f"VERIFICATION FAILED: {len(failed)} check(s) failed")
        return 1
    print("VERIFICATION PASSED (all applicable checks)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
