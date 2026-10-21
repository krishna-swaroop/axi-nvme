"""Run the portable buffer-ownership contract in Icarus and retain evidence."""

from __future__ import annotations

import argparse
import datetime
import hashlib
import importlib.metadata
import json
import platform
from pathlib import Path
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET

from packaging.requirements import Requirement
from packaging.version import Version
from cocotb_tools.runner import get_runner


ROOT = Path(__file__).resolve().parents[1]
RTL = ROOT / "rtl/buffer/buffer_ownership.sv"
TEST_DIR = ROOT / "tests/buffer"
TEST_MODULE = "test_buffer_ownership"
EXPECTED_TEST_NAMES = frozenset({
    "reorder_duplicate_and_old_epoch_handles",
    "abort_quarantines_filling_ready_and_inflight",
    "completion_wins_its_deadline_but_other_slot_can_timeout",
    "simultaneous_distinct_transitions_and_same_slot_conflict",
    "failed_completion_quarantines_owned_slots_and_blocks_new_work",
    "invalid_slot_and_wrong_state_handles_fault",
    "tiny_width_exhaustion_sticky_counters_and_recovery",
    "seeded_random_valid_ownership_cycles",
})
LOCK = ROOT / "requirements-portable.lock"
CONTRACT = ROOT / "docs/contracts/PORTABLE_CAPTURE_V1.md"
WORKFLOW = ROOT / ".github/workflows/buffer.yml"
REPORT_DIR = ROOT / "reports/buffer"
FINAL_JUNIT = REPORT_DIR / "buffer.xml"
MANIFEST = REPORT_DIR / "manifest.json"
TINY_PARAMETERS = {
    "LEASE_WIDTH": 3,
    "EPOCH_WIDTH": 2,
    "COUNTER_WIDTH": 2,
    "TIMEOUT_CYCLES": 4,
}
NOMINAL_PARAMETERS = {
    "LEASE_WIDTH": 64,
    "EPOCH_WIDTH": 32,
    "COUNTER_WIDTH": 32,
    "TIMEOUT_CYCLES": 32,
}
CONFIGURATIONS = (
    {"name": "slots-1-tiny", "parameters": {"SLOTS": 1, **TINY_PARAMETERS}},
    {"name": "slots-3-tiny", "parameters": {"SLOTS": 3, **TINY_PARAMETERS}},
    # Keep enough lifetime leases for the strategy-independent reuse search.
    {"name": "slots-8-tiny", "parameters": {"SLOTS": 8, **TINY_PARAMETERS,
                                                "LEASE_WIDTH": 4}},
    {"name": "slots-8-nominal", "parameters": {"SLOTS": 8, **NOMINAL_PARAMETERS}},
)


class QualificationError(RuntimeError):
    pass


def _utc_now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _source_hashes() -> dict[str, str]:
    paths = (RTL, TEST_DIR / f"{TEST_MODULE}.py", Path(__file__).resolve(),
             CONTRACT, LOCK, WORKFLOW)
    result = {}
    for path in paths:
        if not path.is_file():
            raise QualificationError(f"required qualification source is missing: {path}")
        result[str(path.relative_to(ROOT))] = _sha256(path)
    return result


def _locked_dependency_versions() -> dict[str, dict[str, str | bool]]:
    if not LOCK.is_file():
        raise QualificationError(f"portable dependency lock is missing: {LOCK}")
    dependencies = {}
    for raw in LOCK.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        try:
            requirement = Requirement(line)
        except Exception as exc:
            raise QualificationError(f"invalid portable lock requirement {line!r}: {exc}") from exc
        pins = list(requirement.specifier)
        if requirement.url or requirement.marker or len(pins) != 1 or pins[0].operator != "==":
            raise QualificationError(f"portable lock entry is not one ordinary exact pin: {line!r}")
        expected = pins[0].version
        try:
            actual = importlib.metadata.version(requirement.name)
        except importlib.metadata.PackageNotFoundError as exc:
            raise QualificationError(f"locked dependency is not installed: {requirement.name}") from exc
        matches = Version(actual) == Version(expected)
        key = requirement.name.lower().replace("_", "-")
        dependencies[key] = {"expected": expected, "actual": actual, "matches_lock": matches}
        if not matches:
            raise QualificationError(
                f"installed {requirement.name} {actual} does not match lock {expected}"
            )
    if not dependencies:
        raise QualificationError("portable lock contains no pinned dependencies")
    return dependencies


def _tool_version(executable: str) -> dict:
    path = shutil.which(executable)
    if path is None:
        raise QualificationError(f"required simulator executable is missing: {executable}")
    try:
        result = subprocess.run([path, "-V"], check=False, capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise QualificationError(f"could not query {executable} version: {exc}") from exc
    if result.returncode != 0:
        raise QualificationError(f"{executable} -V exited {result.returncode}: {result.stderr.strip()}")
    return {"path": path, "returncode": result.returncode,
            "stdout": result.stdout.strip(), "stderr": result.stderr.strip()}


def _count_attr(node: ET.Element, name: str) -> int:
    raw = node.get(name, "0")
    try:
        value = int(raw)
    except ValueError as exc:
        raise QualificationError(f"JUnit has invalid {name} attribute {raw!r}") from exc
    if value < 0:
        raise QualificationError(f"JUnit has negative {name} attribute {value}")
    return value


def _junit_summary(
    path: Path, expected_test_names: frozenset[str] | None = None
) -> tuple[ET.Element, dict[str, int]]:
    if not path.is_file():
        raise QualificationError(f"JUnit report was not produced: {path}")
    try:
        root = ET.parse(path).getroot()
    except (ET.ParseError, OSError) as exc:
        raise QualificationError(f"JUnit report is unreadable: {path}: {exc}") from exc
    suites = [root] if root.tag == "testsuite" else list(root.iter("testsuite"))
    if not suites:
        raise QualificationError(f"JUnit report contains no testsuite: {path}")
    cases = list(root.iter("testcase"))
    if not cases:
        raise QualificationError(f"JUnit report contains zero tests: {path}")
    names = [case.get("name") for case in cases]
    if any(not name for name in names):
        raise QualificationError(f"JUnit report contains a test without a name: {path}")
    if expected_test_names is not None:
        actual_names = set(names)
        missing = sorted(expected_test_names - actual_names)
        unexpected = sorted(actual_names - expected_test_names)
        if len(names) != len(expected_test_names) or missing or unexpected:
            raise QualificationError(
                f"JUnit test set mismatch in {path}: missing={missing}, "
                f"unexpected={unexpected}, cases={len(names)}, "
                f"expected={len(expected_test_names)}"
            )
    leaves = [suite for suite in suites if not any(child.tag == "testsuite" for child in suite)]

    def aggregate(name: str) -> int:
        return max(_count_attr(root, name), sum(_count_attr(suite, name) for suite in leaves))

    failures = max(aggregate("failures"), sum(case.find("failure") is not None for case in cases))
    errors = max(aggregate("errors"), sum(case.find("error") is not None for case in cases))
    skipped = max(aggregate("skipped"), sum(case.find("skipped") is not None for case in cases))
    summary = {"tests": len(cases), "failures": failures, "errors": errors, "skipped": skipped,
               "executed": len(cases) - skipped}
    if summary["executed"] <= 0:
        raise QualificationError(f"JUnit report has zero executed tests: {path}")
    if failures or errors or skipped:
        raise QualificationError(
            f"JUnit report has {failures} failures, {errors} errors and {skipped} skips: {path}"
        )
    return root, summary


def _append_suites(aggregate: ET.Element, root: ET.Element) -> None:
    if root.tag == "testsuite":
        aggregate.append(root)
    else:
        for child in list(root):
            if child.tag == "testsuite":
                aggregate.append(child)


def _failure_suite(name: str, message: str) -> ET.Element:
    suite = ET.Element("testsuite", {"name": name, "tests": "1", "failures": "0",
                                      "errors": "1", "skipped": "0"})
    case = ET.SubElement(suite, "testcase", {"name": name, "classname": "buffer.qualification"})
    error = ET.SubElement(case, "error", {"message": message})
    error.text = message
    return suite


def _write_junit(path: Path, suites: list[ET.Element]) -> dict[str, int]:
    aggregate = ET.Element("testsuites")
    for suite in suites:
        aggregate.append(suite)
    cases = list(aggregate.iter("testcase"))
    failures = sum(case.find("failure") is not None for case in cases)
    errors = sum(case.find("error") is not None for case in cases)
    skipped = sum(case.find("skipped") is not None for case in cases)
    aggregate.set("tests", str(len(cases)))
    aggregate.set("failures", str(failures))
    aggregate.set("errors", str(errors))
    aggregate.set("skipped", str(skipped))
    path.parent.mkdir(parents=True, exist_ok=True)
    ET.ElementTree(aggregate).write(path, encoding="utf-8", xml_declaration=True)
    return {"tests": len(cases), "executed": len(cases) - skipped,
            "failures": failures, "errors": errors, "skipped": skipped}


def _git_identity() -> dict:
    def run(args):
        result = subprocess.run(["git", *args], cwd=ROOT, check=False,
                                capture_output=True, text=True, timeout=10)
        if result.returncode:
            return None
        return result.stdout.strip()
    return {"head": run(["rev-parse", "HEAD"]), "status": run(["status", "--porcelain=v1", "--untracked-files=all"])}


def run_qualification(seed: int) -> tuple[int, dict]:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    FINAL_JUNIT.unlink(missing_ok=True)
    MANIFEST.unlink(missing_ok=True)
    for stale in REPORT_DIR.glob("buffer-*.xml"):
        stale.unlink(missing_ok=True)

    manifest = {
        "schema_version": 1,
        "started_at": _utc_now(),
        "status": "running",
        "project": None,
        "python": {"version": sys.version, "executable": sys.executable,
                   "platform": platform.platform()},
        "parameters": [{"profile": config["name"], **config["parameters"]}
                       for config in CONFIGURATIONS],
        "expected_tests_per_profile": sorted(EXPECTED_TEST_NAMES),
        "runs": [],
        "source_sha256": {},
        "failures": [],
    }
    suites: list[ET.Element] = []
    exit_code = 1
    initial_source_hashes = None
    try:
        identity = _git_identity()
        if not identity.get("head") or identity.get("status") is None:
            raise QualificationError("Git head/status identity is unavailable; refusing unbound evidence")
        manifest["project"] = identity
        initial_source_hashes = _source_hashes()
        manifest["source_sha256"] = initial_source_hashes
        manifest["dependencies"] = _locked_dependency_versions()
        manifest["tools"] = {tool: _tool_version(tool) for tool in ("iverilog", "vvp")}
        manifest["seed_base"] = seed

        for index, config in enumerate(CONFIGURATIONS):
            profile = config["name"]
            parameters = dict(config["parameters"])
            slots = parameters["SLOTS"]
            run_seed = seed + (index + 1) * 101 + slots
            build = ROOT / "build/qualification" / f"buffer-{profile}"
            junit = REPORT_DIR / f"buffer-{profile}.xml"
            waveform = build / "buffer_ownership.fst"
            junit.unlink(missing_ok=True)
            waveform.unlink(missing_ok=True)
            build.mkdir(parents=True, exist_ok=True)
            build_log = build / "build.log"
            simulation_log = build / "simulation.log"
            build_log.unlink(missing_ok=True)
            simulation_log.unlink(missing_ok=True)
            record = {"profile": profile, "parameters": parameters, "seed": run_seed,
                      "build_dir": str(build), "junit": str(junit),
                      "waveform": str(waveform), "status": "running"}
            manifest["runs"].append(record)
            try:
                runner = get_runner("icarus")
                runner.build(sources=[RTL], hdl_toplevel="buffer_ownership", build_dir=build,
                             parameters=parameters, always=True, waves=True,
                             timescale=("1ns", "1ps"), log_file=str(build_log))
                runner.test(hdl_toplevel="buffer_ownership", test_module=TEST_MODULE,
                            test_dir=TEST_DIR, results_xml=str(junit), waves=True, seed=run_seed,
                            log_file=str(simulation_log),
                            extra_env={"BUFFER_TEST_TIMEOUT_CYCLES":
                                       str(parameters["TIMEOUT_CYCLES"])})
                if not waveform.is_file():
                    raise QualificationError(f"fresh Icarus waveform was not produced: {waveform}")
                waveform_size = waveform.stat().st_size
                if waveform_size <= 0:
                    raise QualificationError(f"Icarus waveform is empty: {waveform}")
                record["waveform_size_bytes"] = waveform_size
                record["waveform_sha256"] = _sha256(waveform)
                record["logs"] = {}
                for log in (build_log, simulation_log):
                    if not log.is_file() or (log == simulation_log and log.stat().st_size == 0):
                        raise QualificationError(f"current simulator log is missing or empty: {log}")
                    record["logs"][log.name] = {"path": str(log.relative_to(ROOT)),
                                                "size_bytes": log.stat().st_size,
                                                "sha256": _sha256(log)}
                root, summary = _junit_summary(junit, EXPECTED_TEST_NAMES)
                suites.append(root if root.tag == "testsuite" else _wrap_suites(root))
                record["summary"] = summary
                record["status"] = "passed"
            except (Exception, SystemExit) as exc:
                message = f"{profile}: {type(exc).__name__}: {exc}"
                record["status"] = "failed"
                record["error"] = message
                manifest["failures"].append(message)
                if waveform.is_file() and waveform.stat().st_size > 0:
                    record["waveform_size_bytes"] = waveform.stat().st_size
                    record["waveform_sha256"] = _sha256(waveform)
                if junit.is_file():
                    try:
                        root, _ = _junit_summary(junit)
                        suites.append(root if root.tag == "testsuite" else _wrap_suites(root))
                    except Exception:
                        pass
                suites.append(_failure_suite(profile, message))
                break

        if initial_source_hashes is not None:
            final_source_hashes = _source_hashes()
            manifest["source_sha256_after"] = final_source_hashes
            if final_source_hashes != initial_source_hashes:
                raise QualificationError("qualification sources changed while the matrix was running")

        if not manifest["failures"]:
            exit_code = 0
    except (Exception, SystemExit) as exc:
        manifest["failures"].append(f"{type(exc).__name__}: {exc}")
        suites.append(_failure_suite("buffer-runner", manifest["failures"][-1]))
    finally:
        manifest["finished_at"] = _utc_now()
        manifest["status"] = "passed" if exit_code == 0 and not manifest["failures"] else "failed"
        manifest["junit_summary"] = _write_junit(FINAL_JUNIT, suites)
        temporary = MANIFEST.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        temporary.replace(MANIFEST)

    print(f"{manifest['status'].upper()}: {manifest['junit_summary']['executed']} tests executed")
    print(f"JUnit: {FINAL_JUNIT}")
    print(f"Manifest: {MANIFEST}")
    for failure in manifest["failures"]:
        print(f"FAIL: {failure}", file=sys.stderr)
    return exit_code, manifest


def _wrap_suites(root: ET.Element) -> ET.Element:
    """Return an aggregate testsuites node as one nested suite container."""
    wrapper = ET.Element("testsuite", {"name": "buffer-configuration"})
    for child in list(root):
        if child.tag == "testsuite":
            wrapper.append(child)
    return wrapper


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seed", type=int, default=20261021)
    args = parser.parse_args(argv)
    code, _manifest = run_qualification(args.seed)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
