import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

from scripts import qualify as qualification
from scripts.qualify import (
    QualificationError,
    build_python_env,
    load_config,
    parse_development_lock,
    parse_junit,
    run_command,
    validate_junit,
    verify_checkout,
    verify_installed_module_origin,
    verify_python_version_pins,
    validate_pytest_paths,
)


ROOT = Path(__file__).resolve().parents[1]


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def _make_repo(tmp_path: Path) -> tuple[Path, str]:
    repo = tmp_path / "source"
    repo.mkdir()
    subprocess.run(["git", "init", "--quiet", str(repo)], check=True)
    _git(repo, "config", "user.email", "qualification@example.invalid")
    _git(repo, "config", "user.name", "Qualification Test")
    (repo / "source.py").write_text("PINNED = True\n", encoding="utf-8")
    _git(repo, "add", "source.py")
    _git(repo, "commit", "--quiet", "-m", "fixture")
    return repo, _git(repo, "rev-parse", "HEAD")


def _write_junit(path: Path, *, cases: str) -> None:
    path.write_text(f"<testsuite name='fixture'>{cases}</testsuite>\n", encoding="utf-8")


def _make_configured_test_paths(tmp_path: Path, config: dict, *, omit: tuple[str, str] | None = None):
    source_paths = {}
    for repository, item in config["repositories"].items():
        checkout = tmp_path / repository
        source_paths[repository] = checkout
        for relative in item["pytest_paths"]:
            if omit == (repository, relative):
                continue
            candidate = checkout.joinpath(*Path(relative).parts)
            if relative == "test" or relative.endswith("/"):
                candidate.mkdir(parents=True, exist_ok=True)
            else:
                candidate.parent.mkdir(parents=True, exist_ok=True)
                candidate.touch()
    return source_paths


def test_development_config_and_lock_share_immutable_fork_pins(tmp_path: Path):
    config = load_config(ROOT / "config/development.json", ROOT / "requirements-development.lock")
    assert config["repositories"]["litenvme"]["commit"] == config["_lock_pins"]["litenvme"]["commit"]
    assert config["repositories"]["litepcie"]["commit"] == config["_lock_pins"]["litepcie"]["commit"]
    assert config["_lock_pins"]["litex"]["commit"]
    assert config["_lock_pins"]["migen"]["commit"]
    assert config["_version_pins"]["pytest"] == "8.4.2"
    source_paths = _make_configured_test_paths(tmp_path, config)
    selected = validate_pytest_paths(config, source_paths)
    assert selected["litepcie"] == config["repositories"]["litepcie"]["pytest_paths"]
    assert selected["litenvme"] == ["test"]


def test_validate_pytest_paths_rejects_missing_configured_path(tmp_path: Path):
    config = load_config(ROOT / "config/development.json", ROOT / "requirements-development.lock")
    missing = config["repositories"]["litepcie"]["pytest_paths"][0]
    source_paths = _make_configured_test_paths(tmp_path, config, omit=("litepcie", missing))
    with pytest.raises(QualificationError, match="missing or escapes checkout"):
        validate_pytest_paths(config, source_paths)


def test_development_config_rejects_malformed_pytest_path(tmp_path: Path):
    source = json.loads((ROOT / "config/development.json").read_text(encoding="utf-8"))
    source["repositories"]["litepcie"]["pytest_paths"][0] = "../test/test_rootport_cfg_write.py"
    config_path = tmp_path / "development.json"
    config_path.write_text(json.dumps(source), encoding="utf-8")
    with pytest.raises(QualificationError, match="under test/|contain no"):
        load_config(config_path, ROOT / "requirements-development.lock")


def test_development_lock_rejects_unsupported_requirement_syntax(tmp_path: Path):
    lock_path = tmp_path / "requirements.lock"
    lock_path.write_text("pytest>=8.0\n", encoding="utf-8")
    with pytest.raises(QualificationError, match="unsupported development lock syntax"):
        parse_development_lock(lock_path)


def test_ordinary_lock_versions_match_and_are_recorded(monkeypatch):
    calls = []

    def distribution(name):
        calls.append(name)
        return SimpleNamespace(version={"pygments": "2.21.0", "pyyaml": "6.0.2"}[name])

    monkeypatch.setattr(qualification.importlib.metadata, "distribution", distribution)
    manifest = {}
    result = verify_python_version_pins({"pygments": "2.21.0", "pyyaml": "6.0.2"}, manifest=manifest)
    assert calls == ["pygments", "pyyaml"]
    assert result == manifest["python_version_provenance"]
    assert result["pygments"] == {
        "expected_version": "2.21.0", "actual_version": "2.21.0", "matches_lock": True,
    }


def test_ordinary_lock_version_mismatch_is_recorded_and_rejected(monkeypatch):
    monkeypatch.setattr(
        qualification.importlib.metadata, "distribution",
        lambda _name: SimpleNamespace(version="8.3.0"),
    )
    manifest = {}
    with pytest.raises(QualificationError, match="pytest version mismatch"):
        verify_python_version_pins({"pytest": "8.4.2"}, manifest=manifest)
    assert manifest["python_version_provenance"]["pytest"] == {
        "expected_version": "8.4.2", "actual_version": "8.3.0", "matches_lock": False,
    }


def test_ordinary_lock_missing_distribution_is_recorded_and_rejected(monkeypatch):
    def missing(name):
        raise qualification.importlib.metadata.PackageNotFoundError(name)

    monkeypatch.setattr(qualification.importlib.metadata, "distribution", missing)
    manifest = {}
    with pytest.raises(QualificationError, match="missing-package is not installed"):
        verify_python_version_pins({"missing-package": "1.2.3"}, manifest=manifest)
    assert manifest["python_version_provenance"]["missing-package"] == {
        "expected_version": "1.2.3", "actual_version": None, "matches_lock": False,
    }


def test_verify_checkout_accepts_exact_clean_commit_and_ds_store(tmp_path: Path):
    repo, sha = _make_repo(tmp_path)
    (repo / ".DS_Store").write_bytes(b"finder metadata")
    result = verify_checkout(repo, sha)
    assert result["commit"] == sha
    assert result["clean"] is True


def test_verify_checkout_rejects_wrong_sha_and_dirty_source(tmp_path: Path):
    repo, sha = _make_repo(tmp_path)
    other = "0" * 40 if sha != "0" * 40 else "1" * 40
    with pytest.raises(QualificationError, match="expected"):
        verify_checkout(repo, other)

    (repo / "source.py").write_text("PINNED = False\n", encoding="utf-8")
    with pytest.raises(QualificationError, match="changes"):
        verify_checkout(repo, sha)

    _git(repo, "checkout", "--", "source.py")
    (repo / "untracked.py").write_text("unexpected = True\n", encoding="utf-8")
    with pytest.raises(QualificationError, match="untracked.py"):
        verify_checkout(repo, sha)


def test_junit_requires_nonzero_executed_tests(tmp_path: Path):
    report = tmp_path / "zero.xml"
    _write_junit(report, cases="")
    with pytest.raises(QualificationError, match="zero tests"):
        parse_junit(report)


def test_junit_rejects_failures_and_skips(tmp_path: Path):
    failed = tmp_path / "failed.xml"
    _write_junit(failed, cases="<testcase name='bad'><failure message='expected'/></testcase>")
    with pytest.raises(QualificationError, match="1 failures"):
        validate_junit(failed)

    skipped = tmp_path / "skipped.xml"
    _write_junit(skipped, cases="<testcase name='not-run'><skipped/></testcase>")
    with pytest.raises(QualificationError, match="zero executed"):
        validate_junit(skipped)


def test_junit_reports_successful_count(tmp_path: Path):
    report = tmp_path / "passed.xml"
    _write_junit(report, cases="<testcase name='one'/><testcase name='two'/>")
    assert validate_junit(report)["executed"] == 2


def test_nested_junit_counts_cases_once_and_honors_aggregate_failure(tmp_path: Path):
    nested = tmp_path / "nested.xml"
    nested.write_text(
        "<testsuites><testsuite name='outer'><testcase name='one'/>"
        "<testsuite name='inner'><testcase name='two'/></testsuite>"
        "</testsuite></testsuites>",
        encoding="utf-8",
    )
    assert parse_junit(nested)["tests"] == 2

    failed = tmp_path / "aggregate-failure.xml"
    failed.write_text(
        "<testsuites failures='1'><testsuite tests='1' failures='1'>"
        "<testcase name='reported-without-node'/></testsuite></testsuites>",
        encoding="utf-8",
    )
    with pytest.raises(QualificationError, match="1 failures"):
        validate_junit(failed)


def test_pinned_module_location_rejects_pythonpath_shadowing(tmp_path: Path):
    installed = tmp_path / "site-packages" / "litex" / "__init__.py"
    shadow = tmp_path / "shadow" / "litex" / "__init__.py"
    installed.parent.mkdir(parents=True)
    shadow.parent.mkdir(parents=True)
    installed.touch()
    shadow.touch()
    distribution = SimpleNamespace(locate_file=lambda _name: installed)
    with pytest.raises(QualificationError, match="imports from"):
        verify_installed_module_origin("litex", distribution, shadow)
    assert verify_installed_module_origin("litex", distribution, installed) == str(installed.resolve())


def test_subprocess_timeout_is_captured_as_failure(tmp_path: Path):
    manifest = {"commands": []}
    result = run_command(
        "sleeper", [sys.executable, "-c", "import time; time.sleep(1)"],
        cwd=tmp_path, report_dir=tmp_path / "reports", timeout=0.05, manifest=manifest,
    )
    assert result["timed_out"] is True
    assert result["returncode"] is None
    assert "Timed out" in Path(result["stderr"]).read_text(encoding="utf-8")
    assert manifest["commands"][0]["name"] == "sleeper"


def test_pythonpath_selects_both_requested_sources_ahead_of_stale_installs(tmp_path: Path):
    source_root = tmp_path / "forks"
    stale_root = tmp_path / "stale"
    source_paths = {}
    for package in ("litenvme", "litepcie"):
        checkout = source_root / package
        package_dir = checkout / package
        package_dir.mkdir(parents=True)
        (package_dir / "__init__.py").write_text(f"ORIGIN = '{package}'\n", encoding="utf-8")
        stale_package = stale_root / package
        stale_package.mkdir(parents=True)
        (stale_package / "__init__.py").write_text("ORIGIN = 'stale'\n", encoding="utf-8")
        source_paths[package] = checkout

    env = build_python_env(source_paths, {"PATH": os.environ.get("PATH", ""), "PYTHONPATH": str(stale_root)})
    probe = (
        "import importlib.util,json; "
        "print(json.dumps({n:importlib.util.find_spec(n).origin for n in ('litenvme','litepcie')}))"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe], cwd=tmp_path, env=env, capture_output=True, text=True, check=True,
    )
    origins = json.loads(result.stdout)
    for package, checkout in source_paths.items():
        assert Path(origins[package]).resolve().is_relative_to(checkout.resolve())
