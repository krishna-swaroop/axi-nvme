#!/usr/bin/env python3
"""Run the portable capture-stager matrix and generic Yosys checks."""

from __future__ import annotations

from datetime import datetime, timezone
import argparse
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET

from packaging.utils import canonicalize_name
from cocotb_tools.runner import get_runner


ROOT = Path(__file__).resolve().parents[1]
REPORTS = ROOT / "reports" / "capture"
BUILD = ROOT / "build" / "capture"
SEED = int(os.environ.get("COCOTB_RANDOM_SEED", "20261021"))
RTL = ROOT / "rtl" / "stream" / "capture_stager.sv"
TB = ROOT / "tests" / "stream" / "capture_tb.sv"
TEST_MODULE = ROOT / "tests" / "stream" / "test_capture_stager.py"
BOUNDARY_MODULE = ROOT / "tests" / "stream" / "test_capture_boundary.py"
LOCK = ROOT / "requirements-portable.lock"

TEST_CASES = [
    "test_full_rate_sustained",
    "test_chunk_drop_order_and_release_admission",
    "test_partial_disarm_and_rearm",
    "test_reset_while_output_stalled",
    "test_disarm_rejected_partial_without_double_count",
    "test_randomized_sink_stalls",
]
SEQUENCE_TEST = "test_sequence_exhaustion_and_counter_saturation"

PROFILES = [
    {"name": "onebeat_8bit_2banks", "DATA_WIDTH": 8, "CHUNK_BEATS": 1,
     "BANKS": 2, "COUNTER_WIDTH": 16, "SEQUENCE_WIDTH": 64,
     "test_module": "test_capture_stager"},
    {"name": "chunk3_8bit_2banks", "DATA_WIDTH": 8, "CHUNK_BEATS": 3,
     "BANKS": 2, "COUNTER_WIDTH": 16, "SEQUENCE_WIDTH": 64,
     "test_module": "test_capture_stager"},
    {"name": "chunk3_32bit_3banks", "DATA_WIDTH": 32, "CHUNK_BEATS": 3,
     "BANKS": 3, "COUNTER_WIDTH": 16, "SEQUENCE_WIDTH": 64,
     "test_module": "test_capture_stager"},
    {"name": "chunk128_256bit_4banks", "DATA_WIDTH": 256, "CHUNK_BEATS": 128,
     "BANKS": 4, "COUNTER_WIDTH": 64, "SEQUENCE_WIDTH": 64,
     "test_module": "test_capture_stager"},
    {"name": "sequence_counter_boundary", "DATA_WIDTH": 32, "CHUNK_BEATS": 3,
     "BANKS": 3, "COUNTER_WIDTH": 4, "SEQUENCE_WIDTH": 2,
     "test_module": "test_capture_boundary"},
]

EXTRA_HASH_INPUTS = [
    Path("scripts/sim_capture.py"),
    Path(".github/workflows/capture.yml"),
    Path("docs/contracts/PORTABLE_CAPTURE_V1.md"),
    Path("requirements-portable.lock"),
]


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def run_text(argv: list[str], *, timeout: float = 60) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(argv, cwd=ROOT, text=True, stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT, check=False, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        output = exc.stdout or ""
        if isinstance(output, bytes):
            output = output.decode(errors="replace")
        return subprocess.CompletedProcess(argv, 124, f"{output}\nTIMEOUT after {timeout}s")


def qualification_inputs() -> list[Path]:
    inputs = set(EXTRA_HASH_INPUTS)
    for base in (Path("rtl/stream"), Path("tests/stream")):
        directory = ROOT / base
        if not directory.is_dir():
            raise RuntimeError(f"missing qualification source directory: {directory}")
        inputs.update(path.relative_to(ROOT) for path in directory.rglob("*")
                      if path.is_file() and path.suffix in {".sv", ".svh", ".v", ".vh", ".py"})
    for relative in inputs:
        if not (ROOT / relative).is_file():
            raise RuntimeError(f"missing qualification source: {relative}")
    return sorted(inputs, key=str)


def hash_sources() -> dict[str, str]:
    return {str(relative): sha256_file(ROOT / relative) for relative in qualification_inputs()}


def git_identity() -> dict[str, object]:
    values: dict[str, object] = {}
    for key, args in {
        "head": ["rev-parse", "--verify", "HEAD"],
        "branch": ["branch", "--show-current"],
        "origin": ["remote", "get-url", "origin"],
    }.items():
        result = run_text(["git", *args], timeout=20)
        value = result.stdout.strip()
        if result.returncode != 0 or (key != "branch" and not value):
            raise RuntimeError(f"required git identity is unavailable ({key}): {result.stdout.strip()}")
        values[key] = value if value else "<detached>"
    status = run_text(["git", "status", "--porcelain"], timeout=20)
    if status.returncode != 0:
        raise RuntimeError(f"could not read git working-tree identity: {status.stdout.strip()}")
    values["status_porcelain"] = status.stdout.splitlines()
    return values


def check_lock() -> dict[str, str]:
    if not LOCK.is_file():
        raise RuntimeError(f"missing portable dependency lock: {LOCK}")
    pins: dict[str, str] = {}
    for line in LOCK.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        match = re.fullmatch(r"([A-Za-z0-9_.-]+)==([A-Za-z0-9][A-Za-z0-9_.+!-]*)", line)
        if not match:
            raise RuntimeError(f"unrecognized unlocked dependency line: {line!r}")
        name, version = match.groups()
        normalized = canonicalize_name(name)
        if normalized in pins and pins[normalized] != version:
            raise RuntimeError(f"conflicting portable pins for {normalized}")
        pins[normalized] = version

    installed: dict[str, str] = {}
    mismatches = []
    for normalized, expected in pins.items():
        try:
            actual = importlib.metadata.version(normalized)
        except importlib.metadata.PackageNotFoundError:
            actual = "missing"
        installed[normalized] = actual
        if actual != expected:
            mismatches.append(f"{normalized}: expected {expected}, found {actual}")
    if mismatches:
        raise RuntimeError("portable dependency lock mismatch: " + "; ".join(mismatches))
    return installed


def tool_version(name: str, args: list[str]) -> str:
    executable = shutil.which(name)
    if executable is None:
        raise RuntimeError(f"required tool is missing: {name}")
    result = run_text([executable, *args], timeout=30)
    if result.returncode != 0 or not result.stdout.strip():
        raise RuntimeError(f"could not read {name} version (exit {result.returncode}): {result.stdout}")
    return result.stdout.strip()


def parse_result(path: Path) -> dict[str, object]:
    if not path.is_file():
        return {"path": str(path.relative_to(ROOT)), "present": False,
                "tests": 0, "failures": 0, "errors": 1, "skipped": 0}
    tree = ET.parse(path)
    cases = list(tree.getroot().iter("testcase"))
    failures = sum(len(list(case.iter("failure"))) for case in cases)
    errors = sum(len(list(case.iter("error"))) for case in cases)
    skipped = sum(len(list(case.iter("skipped"))) for case in cases)
    return {"path": str(path.relative_to(ROOT)), "present": True,
            "tests": len(cases), "failures": failures, "errors": errors,
            "skipped": skipped,
            "test_names": [case.get("name", "") for case in cases],
            "skipped_test_names": [case.get("name", "") for case in cases
                                   if case.find("skipped") is not None]}


def append_junit(aggregate: ET.Element, path: Path) -> None:
    if not path.is_file():
        return
    root = ET.parse(path).getroot()
    if root.tag == "testsuites":
        for suite in list(root):
            aggregate.append(suite)
    elif root.tag == "testsuite":
        aggregate.append(root)


def write_junit(aggregate: ET.Element, destination: Path) -> None:
    suites = list(aggregate)
    tests = sum(int(suite.get("tests", "0")) for suite in suites)
    failures = sum(int(suite.get("failures", "0")) for suite in suites)
    errors = sum(int(suite.get("errors", "0")) for suite in suites)
    skipped = sum(int(suite.get("skipped", "0")) for suite in suites)
    aggregate.attrib.update({"tests": str(tests), "failures": str(failures),
                             "errors": str(errors), "skipped": str(skipped)})
    ET.indent(aggregate, space="  ")
    ET.ElementTree(aggregate).write(destination, encoding="utf-8", xml_declaration=True)


def yosys_check(report_dir: Path) -> dict[str, object]:
    synth_dir = report_dir / "yosys"
    synth_dir.mkdir(parents=True, exist_ok=True)
    json_path = synth_dir / "capture_stager.json"
    log_path = synth_dir / "capture_stager.log"
    json_path.unlink(missing_ok=True)
    log_path.unlink(missing_ok=True)
    script = "; ".join([
        f"read_verilog -sv {RTL}",
        "hierarchy -check -top capture_stager -chparam DATA_WIDTH 256 "
        "-chparam CHUNK_BEATS 128 -chparam BANKS 4 "
        "-chparam COUNTER_WIDTH 64 -chparam SEQUENCE_WIDTH 64",
        "proc",
        "opt",
        "memory_dff",
        "memory_collect",
        "check -assert",
        "select -assert-none t:$dlatch t:$adlatch",
        "stat",
        f"write_json {json_path}",
    ])
    result = run_text([shutil.which("yosys") or "yosys", "-p", script], timeout=120)
    log_path.write_text(result.stdout, encoding="utf-8")
    if result.returncode != 0:
        raise RuntimeError(f"Yosys structural validation failed; see {log_path}")
    if not json_path.is_file():
        raise RuntimeError("Yosys completed without producing its JSON netlist")

    netlist = json.loads(json_path.read_text(encoding="utf-8"))
    cells = [cell for module in netlist.get("modules", {}).values()
             for cell in module.get("cells", {}).values()]
    memories = [cell for cell in cells if cell.get("type") == "$mem_v2"]
    if not memories:
        raise RuntimeError("Yosys netlist contains no inferred $mem_v2 staging memory")
    sync_reads = []
    for cell in memories:
        raw = cell.get("parameters", {}).get("RD_CLK_ENABLE", "")
        if isinstance(raw, list):
            raw = "".join(str(bit) for bit in raw)
        if isinstance(raw, str):
            sync_reads.append(bool(raw) and set(raw) == {"1"})
        else:
            sync_reads.append(bool(raw))
    if not all(sync_reads):
        raise RuntimeError("Yosys staging memory has an asynchronous read port")
    return {"json": str(json_path.relative_to(ROOT)),
            "log": str(log_path.relative_to(ROOT)),
            "memory_cells": len(memories), "synchronous_read_ports": sync_reads}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--simulation-only", action="store_true",
        help="run Icarus tests without Yosys; manifest status is simulation_passed, never full-gate passed",
    )
    args = parser.parse_args(argv)
    mode = "simulation-only" if args.simulation_only else "full-gate"

    REPORTS.mkdir(parents=True, exist_ok=True)
    BUILD.mkdir(parents=True, exist_ok=True)
    start = datetime.now(timezone.utc)
    manifest: dict[str, object] = {
        "schema": 1,
        "started_utc": start.isoformat(),
        "seed": SEED,
        "python": sys.version,
        "platform": platform.platform(),
        "mode": mode,
        "profiles": PROFILES,
        "tests": [],
        "errors": [],
        "artifacts": [],
    }
    junit = ET.Element("testsuites")
    fail = False

    try:
        for path in [RTL, TB, TEST_MODULE, BOUNDARY_MODULE, LOCK,
                     ROOT / "docs/contracts/PORTABLE_CAPTURE_V1.md",
                     ROOT / ".github/workflows/capture.yml"]:
            if not path.is_file():
                raise RuntimeError(f"required qualification input is missing: {path}")

        manifest["dependencies"] = check_lock()
        identity_before = git_identity()
        sources_before = hash_sources()
        manifest["git_identity_before"] = identity_before
        manifest["source_sha256_before"] = sources_before
        versions = {
            "iverilog": tool_version("iverilog", ["-V"]),
            "vvp": tool_version("vvp", ["-V"]),
        }
        if not args.simulation_only:
            versions["yosys"] = tool_version("yosys", ["-V"])
        manifest["tools"] = versions
        (REPORTS / "tool-versions.txt").write_text(
            "\n".join(f"[{name}]\n{value}" for name, value in versions.items()) + "\n",
            encoding="utf-8",
        )
        if args.simulation_only:
            stale_synthesis = REPORTS / "yosys"
            for path in (stale_synthesis / "capture_stager.json",
                         stale_synthesis / "capture_stager.log"):
                path.unlink(missing_ok=True)

        for profile_index, profile in enumerate(PROFILES):
            name = str(profile["name"])
            parameters = {key: int(profile[key]) for key in
                          ("DATA_WIDTH", "CHUNK_BEATS", "BANKS", "COUNTER_WIDTH", "SEQUENCE_WIDTH")}
            profile_dir = BUILD / name
            profile_dir.mkdir(parents=True, exist_ok=True)
            result_xml = REPORTS / f"{name}.xml"
            log_path = profile_dir / "simulation.log"
            waveform = profile_dir / "capture_tb.fst"
            result_xml.unlink(missing_ok=True)
            log_path.unlink(missing_ok=True)
            waveform.unlink(missing_ok=True)
            env = {
                "CAPTURE_CHUNK_BEATS": str(parameters["CHUNK_BEATS"]),
                "CAPTURE_BANKS": str(parameters["BANKS"]),
                "CAPTURE_COUNTER_WIDTH": str(parameters["COUNTER_WIDTH"]),
                "CAPTURE_SEQUENCE_WIDTH": str(parameters["SEQUENCE_WIDTH"]),
                "COCOTB_RANDOM_SEED": str(SEED + profile_index * 100),
            }
            expected_names = ([SEQUENCE_TEST] if profile["test_module"] == "test_capture_boundary"
                              else list(TEST_CASES))
            profile_record: dict[str, object] = {
                "name": name, "parameters": parameters, "seed": int(env["COCOTB_RANDOM_SEED"]),
                "expected_tests": len(expected_names),
                "expected_test_names": expected_names,
                "test_module": profile["test_module"],
            }
            try:
                runner = get_runner("icarus")
                runner.build(
                    sources=[RTL, TB],
                    hdl_toplevel="capture_tb",
                    parameters=parameters,
                    build_dir=profile_dir,
                    always=True,
                    waves=True,
                    log_file=str(log_path),
                )
                runner.test(
                    hdl_toplevel="capture_tb",
                    test_module=str(profile["test_module"]),
                    test_dir=TEST_MODULE.parent,
                    build_dir=profile_dir,
                    results_xml=str(result_xml),
                    seed=int(env["COCOTB_RANDOM_SEED"]),
                    extra_env=env,
                    waves=True,
                    log_file=str(log_path),
                )
            except (Exception, SystemExit) as exc:  # Preserve reports from the other profiles.
                fail = True
                profile_record["runner_error"] = f"{type(exc).__name__}: {exc}"
                manifest["errors"].append(f"{name}: {profile_record['runner_error']}")  # type: ignore[union-attr]

            result_record = parse_result(result_xml)
            profile_record["result"] = result_record
            expected_tests = int(profile_record["expected_tests"])
            expected_names = list(profile_record["expected_test_names"])
            if (not result_record["present"] or result_record["tests"] != expected_tests
                    or result_record["failures"] or result_record["errors"]
                    or result_record["skipped"]
                    or sorted(result_record.get("test_names", [])) != sorted(expected_names)):
                fail = True
                manifest["errors"].append(  # type: ignore[union-attr]
                    f"{name}: expected exact tests {expected_names} with no skips; result was {result_record}"
                )
            append_junit(junit, result_xml)
            if not log_path.is_file() or log_path.stat().st_size == 0:
                fail = True
                manifest["errors"].append(f"{name}: missing or empty simulator log")  # type: ignore[union-attr]
            if not waveform.is_file() or waveform.stat().st_size == 0:
                fail = True
                manifest["errors"].append(f"{name}: missing or empty FST waveform")  # type: ignore[union-attr]
            profile_record["waveform"] = str(waveform.relative_to(ROOT))
            manifest["tests"].append(profile_record)  # type: ignore[union-attr]

        if args.simulation_only:
            manifest["synthesis"] = {"status": "not_run", "reason": "--simulation-only"}
        else:
            manifest["synthesis"] = yosys_check(REPORTS)
    except Exception as exc:
        fail = True
        manifest["errors"].append(f"{type(exc).__name__}: {exc}")  # type: ignore[union-attr]
    finally:
        write_junit(junit, REPORTS / "capture.xml")
        try:
            identity_after = git_identity()
            sources_after = hash_sources()
            manifest["git_identity_after"] = identity_after
            manifest["source_sha256_after"] = sources_after
            identity_stable = all(
                identity_before[key] == identity_after[key]
                for key in ("head", "branch", "origin")
            ) if "identity_before" in locals() else False
            hashes_stable = sources_before == sources_after if "sources_before" in locals() else False
            manifest["git_identity_stable"] = identity_stable
            manifest["source_hashes_stable"] = hashes_stable
            if not identity_stable:
                fail = True
                manifest["errors"].append("git HEAD, branch, or origin changed during qualification")  # type: ignore[union-attr]
            if not hashes_stable:
                fail = True
                manifest["errors"].append("qualification sources changed during the run")  # type: ignore[union-attr]
        except Exception as exc:
            fail = True
            manifest["errors"].append(f"could not verify post-run source identity: {type(exc).__name__}: {exc}")  # type: ignore[union-attr]
        manifest["junit"] = str((REPORTS / "capture.xml").relative_to(ROOT))
        manifest["finished_utc"] = datetime.now(timezone.utc).isoformat()
        manifest["elapsed_seconds"] = round((datetime.now(timezone.utc) - start).total_seconds(), 3)
        manifest["status"] = "failed" if fail else (
            "simulation_passed" if args.simulation_only else "passed"
        )
        for path in sorted(REPORTS.rglob("*")) + sorted(BUILD.rglob("*.fst")) + sorted(BUILD.rglob("*.log")):
            if path.is_file() and path.name != "manifest.json" and not path.name.endswith(".tmp"):
                manifest["artifacts"].append({  # type: ignore[union-attr]
                    "path": str(path.relative_to(ROOT)), "size_bytes": path.stat().st_size,
                    "sha256": sha256_file(path),
                })
        (REPORTS / "manifest.json").write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )

    print(json.dumps({"status": manifest["status"], "tests": manifest["tests"],
                      "errors": manifest["errors"], "junit": manifest["junit"]}, indent=2))
    return 1 if fail else 0


if __name__ == "__main__":
    raise SystemExit(main())
