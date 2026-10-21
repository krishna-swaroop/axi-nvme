"""Qualify pinned LitePCIe/LiteNVMe forks and generated RTL with evidence."""

from __future__ import annotations

import argparse
import datetime as _datetime
import importlib.metadata
import json
import os
from pathlib import Path, PurePosixPath
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET


ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "config" / "development.json"
LOCK_PATH = ROOT / "requirements-development.lock"
REPORTS = ROOT / "reports" / "development"
REPOSITORY_NAMES = ("litepcie", "litenvme")
PYTHON_DISTRIBUTIONS = ("litex", "migen", "litepcie", "litenvme")
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_DIST_NAME_RE = r"[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?"
_VERSION_RE = r"[A-Za-z0-9][A-Za-z0-9.!+_-]*"


class QualificationError(RuntimeError):
    """A fail-closed qualification precondition or artifact was not met."""


def _utc_now() -> str:
    return _datetime.datetime.now(_datetime.timezone.utc).isoformat()


def _normalized_distribution_name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _normalize_repo_url(url: str) -> str:
    value = url.removeprefix("git+")
    return value[:-1] if value.endswith("/") else value


def parse_development_lock(lock_path: Path) -> tuple[dict[str, dict[str, str]], dict[str, str]]:
    """Parse the deliberately small lock grammar: exact versions or immutable Git refs."""
    pins: dict[str, dict[str, str]] = {}
    version_pins: dict[str, str] = {}
    for line_number, raw_line in enumerate(lock_path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        vcs_match = re.fullmatch(rf"(?P<name>{_DIST_NAME_RE})\s+@\s+(?P<target>\S+)", line)
        if vcs_match:
            name, target = vcs_match.group("name", "target")
            if not target.startswith("git+"):
                raise QualificationError(f"unsupported development lock syntax on line {line_number}: {line}")
            url, separator, commit = target.rpartition("@")
            if not separator or not url.startswith("git+") or not _SHA_RE.fullmatch(commit):
                raise QualificationError(f"VCS lock entry is not pinned to a full SHA on line {line_number}: {line}")
            key = _normalized_distribution_name(name)
            if key in pins or key in version_pins:
                raise QualificationError(f"duplicate development lock entry for {name}")
            pins[key] = {"url": _normalize_repo_url(url), "commit": commit}
            continue

        version_match = re.fullmatch(rf"(?P<name>{_DIST_NAME_RE})==(?P<version>{_VERSION_RE})", line)
        if version_match:
            name, version = version_match.group("name", "version")
            key = _normalized_distribution_name(name)
            if key in pins or key in version_pins:
                raise QualificationError(f"duplicate development lock entry for {name}")
            version_pins[key] = version
            continue

        raise QualificationError(f"unsupported development lock syntax on line {line_number}: {line}")
    if not version_pins:
        raise QualificationError("development lock has no exact ordinary version pins")
    return pins, version_pins


def parse_vcs_lock(lock_path: Path) -> dict[str, dict[str, str]]:
    """Read immutable PEP 508 VCS pins from the development lock."""
    return parse_development_lock(lock_path)[0]


def _validate_pytest_path_lists(repositories: dict) -> dict[str, list[str]]:
    """Validate configured pytest paths before resolving them against checkouts."""
    selected = {}
    for name in REPOSITORY_NAMES:
        repository = repositories.get(name)
        paths = repository.get("pytest_paths") if isinstance(repository, dict) else None
        if not isinstance(paths, list) or not paths:
            raise QualificationError(f"{name} pytest_paths must be a non-empty list")
        if not isinstance(repository.get("qualification_stage"), str) or not repository["qualification_stage"].strip():
            raise QualificationError(f"{name} qualification_stage must be a non-empty string")
        if not isinstance(repository.get("qualification_scope"), str) or not repository["qualification_scope"].strip():
            raise QualificationError(f"{name} qualification_scope must be a non-empty string")
        validated = []
        for value in paths:
            if not isinstance(value, str) or not value.strip() or "\\" in value:
                raise QualificationError(f"{name} pytest path must be a non-empty relative POSIX path: {value!r}")
            path = PurePosixPath(value)
            if path.is_absolute() or ".." in path.parts or not path.parts or path.parts[0] != "test":
                raise QualificationError(f"{name} pytest path must stay under test/ and contain no '..': {value!r}")
            normalized = path.as_posix()
            if normalized != value or normalized in validated:
                raise QualificationError(f"{name} pytest path is non-canonical or duplicated: {value!r}")
            validated.append(normalized)
        selected[name] = validated
    return selected


def validate_pytest_paths(config: dict, source_paths: dict[str, Path]) -> dict[str, list[str]]:
    """Require every configured test path to exist inside its verified checkout."""
    selected = _validate_pytest_path_lists(config.get("repositories", {}))
    for name, paths in selected.items():
        checkout = Path(source_paths[name]).resolve()
        for relative in paths:
            candidate = checkout.joinpath(*PurePosixPath(relative).parts)
            resolved = candidate.resolve()
            if not resolved.is_relative_to(checkout) or not candidate.exists():
                raise QualificationError(f"{name} configured pytest path is missing or escapes checkout: {relative}")
    return selected


def load_config(config_path: Path = CONFIG_PATH, lock_path: Path = LOCK_PATH) -> dict:
    """Load and cross-check the development configuration and dependency lock."""
    config_path = Path(config_path)
    lock_path = Path(lock_path)
    config = json.loads(config_path.read_text(encoding="utf-8"))
    repositories = config.get("repositories")
    if not isinstance(repositories, dict):
        raise QualificationError("development config must contain a repositories object")
    for name in REPOSITORY_NAMES:
        item = repositories.get(name)
        if not isinstance(item, dict):
            raise QualificationError(f"development config is missing repository {name}")
        if not isinstance(item.get("url"), str) or not item["url"].startswith(("https://", "ssh://", "git@")):
            raise QualificationError(f"development config has an invalid URL for {name}")
        if not isinstance(item.get("commit"), str) or not _SHA_RE.fullmatch(item["commit"]):
            raise QualificationError(f"development config {name} commit must be a full 40-character SHA")
    _validate_pytest_path_lists(repositories)

    timeout = config.get("test_timeout_seconds")
    if not isinstance(timeout, int) or isinstance(timeout, bool) or timeout <= 0:
        raise QualificationError("test_timeout_seconds must be a positive integer")

    pins, version_pins = parse_development_lock(lock_path)
    for name in (*REPOSITORY_NAMES, "litex", "migen"):
        key = _normalized_distribution_name(name)
        if key not in pins:
            raise QualificationError(f"development lock is missing VCS pin for {name}")
    for name in REPOSITORY_NAMES:
        pin = pins[_normalized_distribution_name(name)]
        repository = repositories[name]
        if pin["commit"] != repository["commit"] or pin["url"] != _normalize_repo_url(repository["url"]):
            raise QualificationError(f"{name} config and development lock do not agree")

    config["_config_path"] = str(config_path.resolve())
    config["_lock_path"] = str(lock_path.resolve())
    config["_lock_pins"] = pins
    config["_version_pins"] = version_pins
    return config


def run_command(
    name: str,
    args: list[str],
    *,
    cwd: Path,
    report_dir: Path,
    timeout: float | None,
    env: dict[str, str] | None = None,
    manifest: dict | None = None,
) -> dict:
    """Run an argument vector, preserving stdout and stderr as separate files."""
    if not isinstance(args, (list, tuple)) or not args or not all(isinstance(arg, str) for arg in args):
        raise TypeError("commands must be non-empty argument arrays of strings")
    report_dir = Path(report_dir)
    report_dir.mkdir(parents=True, exist_ok=True)
    index = len(manifest.get("commands", [])) + 1 if manifest is not None else 1
    safe_name = re.sub(r"[^A-Za-z0-9_.-]+", "_", name)
    stem = f"{index:02d}-{safe_name}"
    stdout_path = report_dir / f"{stem}.stdout.log"
    stderr_path = report_dir / f"{stem}.stderr.log"
    entry = {
        "name": name,
        "argv": list(args),
        "cwd": str(Path(cwd).resolve()),
        "timeout_seconds": timeout,
        "started_at": _utc_now(),
        "stdout": str(stdout_path.resolve()),
        "stderr": str(stderr_path.resolve()),
        "returncode": None,
        "timed_out": False,
        "launch_error": None,
    }
    started = time.monotonic()
    try:
        with stdout_path.open("wb") as stdout, stderr_path.open("wb") as stderr:
            result = subprocess.run(
                list(args), cwd=str(cwd), env=env, stdout=stdout, stderr=stderr,
                timeout=timeout, check=False,
            )
        entry["returncode"] = result.returncode
    except subprocess.TimeoutExpired as exc:
        entry["timed_out"] = True
        entry["timeout_detail"] = str(exc)
        with stderr_path.open("ab") as stderr:
            stderr.write(f"\nTimed out after {timeout} seconds.\n".encode())
    except OSError as exc:
        entry["launch_error"] = f"{type(exc).__name__}: {exc}"
        with stderr_path.open("ab") as stderr:
            stderr.write((entry["launch_error"] + "\n").encode())
    entry["duration_seconds"] = round(time.monotonic() - started, 3)
    entry["finished_at"] = _utc_now()
    if manifest is not None:
        manifest.setdefault("commands", []).append(entry)
    return entry


def verify_checkout(repo_path: Path, expected_sha: str) -> dict:
    """Verify exact HEAD and reject any tracked or untracked source edits."""
    repo_path = Path(repo_path).resolve()
    if not _SHA_RE.fullmatch(expected_sha):
        raise QualificationError(f"expected revision is not a full SHA: {expected_sha}")

    def git(args: list[str]) -> subprocess.CompletedProcess:
        try:
            result = subprocess.run(
                ["git", "-C", str(repo_path), *args], capture_output=True, check=False,
            )
        except OSError as exc:
            raise QualificationError(f"cannot run git for {repo_path}: {exc}") from exc
        if result.returncode:
            detail = result.stderr.decode("utf-8", "replace").strip()
            raise QualificationError(f"git {' '.join(args)} failed in {repo_path}: {detail}")
        return result

    head = git(["rev-parse", "HEAD"]).stdout.decode("ascii", "strict").strip()
    if head != expected_sha:
        raise QualificationError(f"{repo_path} is at {head}, expected {expected_sha}")
    status = git(["status", "--porcelain=v1", "-z", "--untracked-files=all", "--ignore-submodules=none"]).stdout
    changes = []
    for record in status.split(b"\0"):
        if not record:
            continue
        status_code = record[:2].decode("ascii", "replace")
        path = record[3:].decode("utf-8", "surrogateescape")
        if status_code == "??" and PurePosixPath(path).name == ".DS_Store":
            continue
        changes.append({"status": status_code, "path": path})
    if changes:
        raise QualificationError(f"{repo_path} has tracked or untracked changes: {changes}")
    return {"path": str(repo_path), "commit": head, "clean": True}


def verify_python_provenance(
    config: dict,
    *,
    source_paths: dict[str, Path],
    import_origins: dict[str, str],
) -> dict:
    """Verify installed dependency pins and the effective source for both forks."""
    result = {}
    pins = config["_lock_pins"]
    for name in PYTHON_DISTRIBUTIONS:
        key = _normalized_distribution_name(name)
        expected = pins.get(key)
        if expected is None:
            raise QualificationError(f"development lock has no VCS provenance pin for {name}")
        if name in REPOSITORY_NAMES:
            source = Path(source_paths[name]).resolve()
            origin = Path(import_origins.get(name, "")).resolve()
            if not origin.is_relative_to(source):
                raise QualificationError(f"{name} does not import from its verified source checkout")
            expected = {
                "url": _normalize_repo_url(config["repositories"][name]["url"]),
                "commit": config["repositories"][name]["commit"],
            }
        try:
            distribution = importlib.metadata.distribution(name)
        except importlib.metadata.PackageNotFoundError as exc:
            if name not in REPOSITORY_NAMES:
                raise QualificationError(f"required Python distribution is not installed: {name}") from exc
            distribution = None
        raw_direct_url = distribution.read_text("direct_url.json") if distribution else None
        installed = {"available": distribution is not None, "matches_lock": False}
        if distribution:
            installed["version"] = distribution.version
        if raw_direct_url:
            try:
                direct_url = json.loads(raw_direct_url)
                vcs = direct_url["vcs_info"]
                actual_url = _normalize_repo_url(direct_url["url"])
                actual_commit = vcs["commit_id"]
                vcs_kind = vcs["vcs"]
            except (KeyError, TypeError, json.JSONDecodeError) as exc:
                raise QualificationError(f"{name} has malformed installed VCS provenance") from exc
            installed.update({"url": actual_url, "commit": actual_commit, "vcs": vcs_kind})
            installed["matches_lock"] = (
                vcs_kind == "git" and actual_url == expected["url"] and actual_commit == expected["commit"]
            )

        if name in REPOSITORY_NAMES:
            result[name] = {
                "installed_distribution": installed,
                "effective_source": "verified-checkout",
                "effective_url": expected["url"],
                "effective_commit": expected["commit"],
                "effective_provenance_matches_lock": True,
                "import_origin": str(Path(import_origins[name]).resolve()),
            }
            continue

        if not raw_direct_url:
            raise QualificationError(f"{name} lacks installed VCS provenance (direct_url.json)")
        if not installed["matches_lock"]:
            raise QualificationError(
                f"{name} provenance mismatch: got {installed.get('url')}@{installed.get('commit')}, "
                f"expected {expected['url']}@{expected['commit']}"
            )
        imported_module = verify_installed_module_origin(name, distribution, import_origins[name])
        result[name] = {
            "installed_distribution": installed,
            "effective_source": "installed-vcs-distribution",
            "effective_url": expected["url"],
            "effective_commit": expected["commit"],
            "effective_provenance_matches_lock": True,
            "import_origin": imported_module,
        }
    return result


def verify_python_version_pins(version_pins: dict[str, str], *, manifest: dict | None = None) -> dict:
    """Check every exact ordinary lock pin and record expected/installed versions."""
    records = {}
    failures = []
    for name, expected in version_pins.items():
        try:
            distribution = importlib.metadata.distribution(name)
        except importlib.metadata.PackageNotFoundError:
            actual = None
            matches = False
        else:
            actual = distribution.version
            matches = actual == expected
        records[name] = {
            "expected_version": expected,
            "actual_version": actual,
            "matches_lock": matches,
        }
        if not matches:
            if actual is None:
                failures.append(f"{name} is not installed (expected {expected})")
            else:
                failures.append(f"{name} version mismatch: got {actual}, expected {expected}")

    if manifest is not None:
        manifest["python_version_provenance"] = records
    if failures:
        raise QualificationError("ordinary Python version pin validation failed: " + "; ".join(failures))
    return records


def build_python_env(source_paths: dict[str, Path], base_env: dict[str, str] | None = None) -> dict[str, str]:
    """Put both verified fork checkouts first on PYTHONPATH for every test stage."""
    env = dict(os.environ if base_env is None else base_env)
    ordered_paths = [str(Path(source_paths[name]).resolve()) for name in ("litenvme", "litepcie")]
    inherited = env.get("PYTHONPATH")
    if inherited:
        ordered_paths.extend(entry for entry in inherited.split(os.pathsep) if entry)
    env["PYTHONPATH"] = os.pathsep.join(ordered_paths)
    return env


def verify_installed_module_origin(name: str, distribution, import_origin: str | Path) -> str:
    """Reject PYTHONPATH shadowing of LiteX/Migen's pinned installations."""
    try:
        expected = Path(distribution.locate_file(f"{name}/__init__.py")).resolve()
        imported = Path(import_origin).resolve()
    except (KeyError, TypeError, ValueError) as exc:
        raise QualificationError(f"cannot locate imported module for {name}") from exc
    if imported != expected:
        raise QualificationError(
            f"{name} imports from {imported}, but its pinned distribution is at {expected}"
        )
    return str(imported)


def parse_junit(path: Path) -> dict:
    """Summarize a JUnit XML file; raise for absent, malformed, or empty reports."""
    path = Path(path)
    if not path.is_file():
        raise QualificationError(f"JUnit report was not produced: {path}")
    try:
        root = ET.parse(path).getroot()
    except (ET.ParseError, OSError) as exc:
        raise QualificationError(f"JUnit report is unreadable: {path}: {exc}") from exc
    suites = [root] if root.tag == "testsuite" else list(root.iter("testsuite"))
    if not suites:
        raise QualificationError(f"JUnit report contains no testsuite: {path}")
    # Walk from the document root once. Walking every nested suite recursively
    # counts its child cases repeatedly when JUnit suites are nested.
    cases = list(root.iter("testcase"))
    if not cases:
        raise QualificationError(f"JUnit report contains zero tests: {path}")

    def count_attribute(element: ET.Element, name: str) -> int:
        value = element.get(name)
        if value is None:
            return 0
        try:
            count = int(value)
        except ValueError as exc:
            raise QualificationError(f"JUnit report has invalid {name} count: {value!r}") from exc
        if count < 0:
            raise QualificationError(f"JUnit report has negative {name} count: {count}")
        return count

    def reported_total(name: str) -> int:
        root_count = count_attribute(root, name)
        # If aggregate attributes are missing or misleading, suite-level
        # counters still make a recorded failure visible. Only leaf suites are
        # summed so nested suites do not inflate the total.
        leaf_suites = [suite for suite in suites if not any(child.tag == "testsuite" for child in suite)]
        leaf_count = sum(count_attribute(suite, name) for suite in leaf_suites)
        return max(root_count, leaf_count)

    failures = max(reported_total("failures"), sum(1 for case in cases if case.find("failure") is not None))
    errors = max(reported_total("errors"), sum(1 for case in cases if case.find("error") is not None))
    skipped = max(reported_total("skipped"), sum(1 for case in cases if case.find("skipped") is not None))
    return {
        "path": str(path.resolve()),
        "tests": len(cases),
        "executed": len(cases) - skipped,
        "failures": failures,
        "errors": errors,
        "skipped": skipped,
    }


def validate_junit(path: Path) -> dict:
    summary = parse_junit(path)
    if summary["executed"] <= 0:
        raise QualificationError(f"JUnit report has zero executed tests: {path}")
    if summary["skipped"]:
        raise QualificationError(f"JUnit report has {summary['skipped']} skipped tests: {path}")
    if summary["failures"] or summary["errors"]:
        raise QualificationError(
            f"JUnit report has {summary['failures']} failures and {summary['errors']} errors: {path}"
        )
    return summary


def _clear_stale_junit_reports(report_dir: Path) -> None:
    for name in ("litepcie.xml", "litenvme.xml", "generated.xml", "generated-manifest.json"):
        (report_dir / name).unlink(missing_ok=True)
    for path in report_dir.glob("generated-*.xml"):
        path.unlink(missing_ok=True)


def _record_failure(manifest: dict, message: str) -> None:
    manifest.setdefault("failures", []).append(message)


def _run_checked(
    manifest: dict,
    *,
    name: str,
    args: list[str],
    cwd: Path,
    reports: Path,
    deadline: float,
    env: dict[str, str] | None = None,
) -> dict:
    remaining = max(0.0, deadline - time.monotonic())
    result = run_command(name, args, cwd=cwd, report_dir=reports, timeout=remaining, env=env, manifest=manifest)
    if result["timed_out"]:
        _record_failure(manifest, f"{name} timed out after {remaining:.3f} seconds")
    elif result["launch_error"]:
        _record_failure(manifest, f"{name} could not start: {result['launch_error']}")
    elif result["returncode"] != 0:
        _record_failure(manifest, f"{name} exited with status {result['returncode']}")
    return result


def _read_import_origins(
    source_paths: dict[str, Path], env: dict[str, str], reports: Path, manifest: dict,
) -> dict[str, str]:
    probe = (
        "import importlib.util,json; "
        "names=('litenvme','litepcie','litex','migen'); "
        "print(json.dumps({n:(lambda s: s.origin if s else None)(importlib.util.find_spec(n)) for n in names}))"
    )
    entry = run_command(
        "python-import-origins", [sys.executable, "-c", probe], cwd=ROOT,
        report_dir=reports, timeout=30, env=env, manifest=manifest,
    )
    if entry["returncode"] != 0 or entry["timed_out"] or entry["launch_error"]:
        raise QualificationError("could not probe Python imports; see command logs")
    try:
        origins = json.loads(Path(entry["stdout"]).read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        raise QualificationError(f"Python import probe returned invalid output: {exc}") from exc
    for package in ("litenvme", "litepcie"):
        origin = origins.get(package)
        if not origin:
            raise QualificationError(f"Python cannot locate package {package}")
        resolved_origin = Path(origin).resolve()
        checkout = Path(source_paths[package]).resolve()
        if not resolved_origin.is_relative_to(checkout):
            raise QualificationError(
                f"{package} imports from {resolved_origin}, outside verified source {checkout}"
            )
        origins[package] = str(resolved_origin)
    return origins


def _fetch_checkout(
    name: str, destination: Path, repository: dict, reports: Path, manifest: dict,
) -> dict:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        return verify_checkout(destination, repository["commit"])

    with tempfile.TemporaryDirectory(prefix=f"{name}-fetch-", dir=destination.parent) as temp_dir:
        staging = Path(temp_dir) / "checkout"
        init = run_command(
            f"git-init-{name}", ["git", "init", "--quiet", str(staging)], cwd=ROOT,
            report_dir=reports, timeout=60, manifest=manifest,
        )
        if init["returncode"] != 0:
            raise QualificationError(f"could not initialize temporary {name} checkout")
        for command_name, args in (
            (f"git-origin-{name}", ["git", "-C", str(staging), "remote", "add", "origin", repository["url"]]),
            (f"git-fetch-{name}", ["git", "-C", str(staging), "fetch", "--depth", "1", "origin", repository["commit"]]),
            (f"git-checkout-{name}", ["git", "-C", str(staging), "checkout", "--detach", "FETCH_HEAD"]),
        ):
            result = run_command(command_name, args, cwd=ROOT, report_dir=reports, timeout=300, manifest=manifest)
            if result["returncode"] != 0:
                raise QualificationError(f"could not fetch immutable {name} source; see command logs")
        checkout = verify_checkout(staging, repository["commit"])
        if destination.exists():
            raise QualificationError(f"destination appeared while preparing {name}: {destination}")
        os.replace(staging, destination)
        checkout["path"] = str(destination.resolve())
        return checkout


def _write_manifest(path: Path, manifest: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def qualify(
    *,
    config_path: Path = CONFIG_PATH,
    lock_path: Path = LOCK_PATH,
    source_root: Path | None = None,
    report_dir: Path = REPORTS,
) -> tuple[int, dict]:
    report_dir = Path(report_dir).resolve()
    manifest_path = report_dir / "manifest.json"
    manifest = {
        "schema_version": 1,
        "started_at": _utc_now(),
        "status": "running",
        "project_root": str(ROOT),
        "python": sys.version,
        "python_executable": sys.executable,
        "platform": platform.platform(),
        "config_path": str(Path(config_path).resolve()),
        "lock_path": str(Path(lock_path).resolve()),
        "source_mode": "source-root" if source_root else "fetched-checkouts",
        "source_root": str(Path(source_root).resolve()) if source_root else None,
        "repositories": {},
        "python_provenance": {},
        "python_version_provenance": {},
        "python_imports": {},
        "commands": [],
        "suite_reports": [],
        "failures": [],
    }
    report_dir.mkdir(parents=True, exist_ok=True)
    exit_code = 1
    try:
        _clear_stale_junit_reports(report_dir)
        config = load_config(Path(config_path), Path(lock_path))
        manifest["config"] = {k: v for k, v in config.items() if not k.startswith("_")}
        manifest["dependency_lock_pins"] = config["_lock_pins"]
        manifest["dependency_lock_version_pins"] = config["_version_pins"]

        git = shutil.which("git")
        if not git:
            raise QualificationError("required tool is missing: git")
        manifest["tools"] = {"git": git, "python": sys.executable}

        project_head = run_command(
            "git-project-head", ["git", "rev-parse", "HEAD"], cwd=ROOT,
            report_dir=report_dir, timeout=30, manifest=manifest,
        )
        project_status = run_command(
            "git-project-status", ["git", "status", "--porcelain=v1", "--untracked-files=all"], cwd=ROOT,
            report_dir=report_dir, timeout=30, manifest=manifest,
        )
        if project_head["returncode"] != 0 or project_status["returncode"] != 0:
            raise QualificationError("could not record the qualification project Git state")
        status_text = Path(project_status["stdout"]).read_text(encoding="utf-8", errors="replace")
        manifest["project_git"] = {
            "head": Path(project_head["stdout"]).read_text(encoding="ascii").strip(),
            "dirty": bool(status_text.strip()),
            "status_porcelain": status_text.splitlines(),
        }

        pip_freeze = run_command(
            "pip-freeze", [sys.executable, "-m", "pip", "freeze", "--all"], cwd=ROOT,
            report_dir=report_dir, timeout=30, manifest=manifest,
        )
        if pip_freeze["returncode"] != 0:
            raise QualificationError("could not record installed Python package versions")
        manifest["installed_python_packages"] = Path(pip_freeze["stdout"]).read_text(
            encoding="utf-8", errors="replace",
        ).splitlines()
        verify_python_version_pins(config["_version_pins"], manifest=manifest)
        source_paths: dict[str, Path] = {}
        if source_root is not None:
            source_base = Path(source_root).resolve()
            for name in REPOSITORY_NAMES:
                checkout = verify_checkout(source_base / name, config["repositories"][name]["commit"])
                manifest["repositories"][name] = checkout | config["repositories"][name]
                source_paths[name] = Path(checkout["path"])
        else:
            deps = ROOT / ".deps" / "development"
            for name in REPOSITORY_NAMES:
                checkout = _fetch_checkout(name, deps / name, config["repositories"][name], report_dir, manifest)
                manifest["repositories"][name] = checkout | config["repositories"][name]
                source_paths[name] = Path(checkout["path"])

        env = build_python_env(source_paths)
        manifest["pythonpath"] = env.get("PYTHONPATH", "")
        manifest["python_imports"] = _read_import_origins(source_paths, env, report_dir, manifest)
        manifest["python_provenance"] = verify_python_provenance(
            config, source_paths=source_paths, import_origins=manifest["python_imports"],
        )
        selected_test_paths = validate_pytest_paths(config, source_paths)
        manifest["test_selection"] = {
            name: {
                "stage": config["repositories"][name]["qualification_stage"],
                "scope": config["repositories"][name]["qualification_scope"],
                "pytest_paths": selected_test_paths[name],
            }
            for name in REPOSITORY_NAMES
        }

        deadline = time.monotonic() + config["test_timeout_seconds"]
        suite_commands = []
        for name in REPOSITORY_NAMES:
            repository_config = config["repositories"][name]
            suite_commands.append((
                name,
                repository_config["qualification_stage"],
                source_paths[name],
                selected_test_paths[name],
                report_dir / f"{name}.xml",
            ))
        for name, stage, checkout, pytest_paths, junit_path in suite_commands:
            stage_env = dict(env)
            if name == "litenvme":
                # Retain initialization/transfer progress even if a long model times out.
                stage_env["LITENVME_SIM_VERBOSE"] = "1"
            result = _run_checked(
                manifest,
                name=f"pytest-{name}",
                args=[
                    sys.executable, "-m", "pytest", "-v", "--tb=short",
                    "--capture=tee-sys", "--durations=10",
                    f"--junitxml={junit_path}", *pytest_paths,
                ],
                cwd=checkout,
                reports=report_dir,
                deadline=deadline,
                env=stage_env,
            )
            manifest.setdefault("stages", {})[stage] = {
                "repository": name,
                "pytest_paths": pytest_paths,
                "returncode": result["returncode"],
                "timed_out": result["timed_out"],
            }
            try:
                junit = parse_junit(junit_path)
                junit["suite"] = stage
                manifest["suite_reports"].append(junit)
                manifest.setdefault("stage_counts", {})[stage] = {
                    key: junit[key] for key in ("tests", "executed", "failures", "errors", "skipped")
                }
                validate_junit(junit_path)
            except QualificationError as exc:
                _record_failure(manifest, str(exc))
                manifest["stages"][stage]["status"] = "failed"
            else:
                command_passed = (
                    result["returncode"] == 0 and not result["timed_out"] and not result["launch_error"]
                )
                manifest["stages"][stage]["status"] = "passed" if command_passed else "failed"

        generated_junit = report_dir / "generated.xml"
        result = _run_checked(
            manifest,
            name="qualify-generated",
            args=[sys.executable, "scripts/qualify_generated.py", "--junitxml", str(generated_junit)],
            cwd=ROOT,
            reports=report_dir,
            deadline=deadline,
            env=env,
        )
        manifest.setdefault("stages", {})["generated-rtl"] = {
            "command": "scripts/qualify_generated.py",
            "junitxml": str(generated_junit),
            "returncode": result["returncode"],
            "timed_out": result["timed_out"],
        }
        try:
            junit = parse_junit(generated_junit)
            junit["suite"] = "generated-rtl"
            manifest["suite_reports"].append(junit)
            manifest.setdefault("stage_counts", {})["generated-rtl"] = {
                key: junit[key] for key in ("tests", "executed", "failures", "errors", "skipped")
            }
            validate_junit(generated_junit)
            command_passed = (
                result["returncode"] == 0 and not result["timed_out"] and not result["launch_error"]
            )
            manifest["stages"]["generated-rtl"]["status"] = "passed" if command_passed else "failed"
        except QualificationError as exc:
            _record_failure(manifest, str(exc))
            manifest["stages"]["generated-rtl"]["status"] = "failed"

        manifest["test_totals"] = {
            key: sum(report[key] for report in manifest["suite_reports"])
            for key in ("tests", "executed", "failures", "errors", "skipped")
        }
        manifest["effective_sources"] = {
            name: {
                "path": source["path"],
                "url": source["url"],
                "commit": source["commit"],
                "clean": source["clean"],
            }
            for name, source in manifest["repositories"].items()
        }
        # Reject evidence if a long-running agent changed a dependency mid-run.
        for name in REPOSITORY_NAMES:
            verify_checkout(source_paths[name], config["repositories"][name]["commit"])
        if not manifest["failures"]:
            exit_code = 0
    except Exception as exc:
        _record_failure(manifest, f"{type(exc).__name__}: {exc}")
    finally:
        manifest["finished_at"] = _utc_now()
        manifest["status"] = "passed" if exit_code == 0 and not manifest["failures"] else "failed"
        if "test_totals" not in manifest:
            manifest["test_totals"] = {
                key: sum(report[key] for report in manifest["suite_reports"])
                for key in ("tests", "executed", "failures", "errors", "skipped")
            }
        _write_manifest(manifest_path, manifest)
    return exit_code, manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source-root", type=Path,
        help="parent directory containing sibling litenvme/ and litepcie/ checkouts",
    )
    parser.add_argument("--config", type=Path, default=CONFIG_PATH)
    parser.add_argument("--lock", type=Path, default=LOCK_PATH)
    parser.add_argument("--report-dir", type=Path, default=REPORTS)
    args = parser.parse_args(argv)
    code, manifest = qualify(
        config_path=args.config, lock_path=args.lock, source_root=args.source_root,
        report_dir=args.report_dir,
    )
    print(f"{manifest['status'].upper()}: {manifest['test_totals']['executed']} tests executed")
    print(f"Manifest: {Path(args.report_dir).resolve() / 'manifest.json'}")
    for failure in manifest["failures"]:
        print(f"FAIL: {failure}", file=sys.stderr)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
