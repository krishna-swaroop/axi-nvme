#!/usr/bin/env python3
"""Qualify the portable writer against the pinned real shared-memory responder."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import signal
import subprocess
import sys
import time
import xml.etree.ElementTree as ET

import qualify as provenance

ROOT = Path(__file__).resolve().parents[1]
REPORTS = ROOT / "reports/ddr"
BUILD = ROOT / "build/ddr"
PCIE_BASE = 0x10000000
PARAMETERS = dict(SLOT_STRIDE_BYTES=4096, APERTURE_BYTES=65536,
                  MAX_BURST_BEATS=16, LEASE_WIDTH=64, EPOCH_WIDTH=32, SEQUENCE_WIDTH=64)
PROFILES = {
    f"boundary{width}": dict(PARAMETERS, DATA_WIDTH=width, CHUNK_BEATS=19,
                             SLOTS=3, REGION_BASE=4096 - width // 8)
    for width in (64, 128, 256)
}
PROFILES["nominal256"] = dict(PARAMETERS, DATA_WIDTH=256, CHUNK_BEATS=128,
                               SLOTS=8, REGION_BASE=0)
EXPECTED_PROFILES = ("boundary64", "boundary128", "boundary256", "nominal256")
# Frozen by the independent test workpack before publication; not inferred from DUT/test imports.
EXPECTED_TESTS: tuple[str, ...] = ('test_shared_ram_byte_exact_readback_visibility_and_4k', 'test_bad_job_range_or_zero_lease_fails_before_axi', 'test_framing_faults_reject_early_last_and_sequence_mismatch', 'test_missing_tlast_on_final_beat_faults_then_drains_residue', 'test_axi_bresp_error_faults_before_commit_and_later_bursts', 'test_axi_bid_error_faults_before_commit_and_later_bursts', 'test_abort_during_collect_discards_prefix_and_recovers_clean_handle', 'test_abort_finishes_stalled_advertised_burst_then_drains', 'test_abort_while_waiting_delayed_b_drains_source', 'test_abort_cancels_unpublished_stalled_commit', 'test_responder_propagates_axi_read_error_to_completion')


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def sources() -> dict[str, str]:
    paths = [Path("scripts/sim_ddr.py"), Path("scripts/qualify.py"),
             Path("gateware/hostmem_ddr_fixture.py"), Path(".github/workflows/ddr.yml"),
             Path("docs/contracts/PL_DDR_W08A.md"), Path("config/development.json"),
             Path("docs/hardware/PZ17_TARGET.md"),
             Path("requirements-development.lock")]
    for directory in ("rtl/memory", "tests/memory"):
        paths.extend(p.relative_to(ROOT) for p in (ROOT / directory).rglob("*")
                     if p.is_file() and p.suffix in (".sv", ".v", ".py"))
    for path in paths:
        if not (ROOT / path).is_file():
            raise RuntimeError(f"missing qualification input: {path}")
    return {str(p): sha(ROOT / p) for p in sorted(set(paths))}


def git_identity() -> dict:
    result = {}
    for key, args in (("head", ["rev-parse", "HEAD"]),
                      ("origin", ["remote", "get-url", "origin"]),
                      ("status", ["status", "--porcelain=v1", "--untracked-files=all"])):
        cp = subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, timeout=10)
        if cp.returncode or (key != "status" and not cp.stdout.strip()):
            raise RuntimeError(f"missing Git identity: {key}")
        result[key] = cp.stdout.strip()
    return result


def step(name: str, argv: list[str], manifest: dict, *, env=None, timeout=180) -> dict:
    """Bound a whole simulator process group, preserving logs even on timeout."""
    stem = REPORTS / f"{len(manifest['commands']) + 1:02d}-{name}"
    record = dict(name=name, argv=argv, timeout_seconds=timeout,
                  stdout=str(stem.with_suffix(".stdout.log").relative_to(ROOT)),
                  stderr=str(stem.with_suffix(".stderr.log").relative_to(ROOT)),
                  returncode=None, timed_out=False)
    manifest["commands"].append(record)
    started = time.monotonic()
    with (ROOT / record["stdout"]).open("w") as out, (ROOT / record["stderr"]).open("w") as err:
        process = subprocess.Popen(argv, cwd=ROOT, env=env, stdout=out, stderr=err,
                                   start_new_session=True)
        try:
            record["returncode"] = process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            record["timed_out"] = True
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
            record["returncode"] = process.returncode
    record["elapsed_seconds"] = round(time.monotonic() - started, 3)
    if record["returncode"] or record["timed_out"]:
        raise RuntimeError(f"{name} failed; see {record['stderr']}")
    return record


def worker(profile: str, generated: Path, seed: int) -> None:
    from cocotb_tools.runner import get_runner
    build = BUILD / profile
    parameters = PROFILES[profile]
    runner = get_runner("icarus")
    runner.build(sources=[ROOT / "rtl/memory/capture_ddr_writer.sv", generated,
                          ROOT / "tests/memory/ddr_tb.sv"],
                 hdl_toplevel="ddr_tb", parameters=parameters, build_dir=build,
                 always=True, waves=True, timescale=("1ns", "1ps"))
    env = {"DDR_" + key: str(value) for key, value in parameters.items()}
    env["DDR_PCIE_BASE"] = str(PCIE_BASE)
    env["DDR_SEED"] = str(seed)
    runner.test(hdl_toplevel="ddr_tb", test_module="test_capture_ddr",
                test_dir=ROOT / "tests/memory", build_dir=build,
                results_xml=str(REPORTS / f"{profile}.xml"), waves=True, seed=seed,
                extra_env=env)


def aggregate(suites: list[ET.Element], errors: list[str]) -> dict:
    root = ET.Element("testsuites")
    for suite in suites:
        if suite.tag == "testsuite":
            root.append(suite)
        else:
            root.extend(list(suite))
    for index, message in enumerate(errors):
        suite = ET.SubElement(root, "testsuite", name="ddr-runner")
        case = ET.SubElement(suite, "testcase", name=f"runner-error-{index}")
        ET.SubElement(case, "error", message=message).text = message
    cases = list(root.iter("testcase"))
    totals = dict(tests=len(cases), failures=len(list(root.iter("failure"))),
                  errors=len(list(root.iter("error"))), skipped=len(list(root.iter("skipped"))))
    root.attrib.update({k: str(v) for k, v in totals.items()})
    ET.indent(root)
    ET.ElementTree(root).write(REPORTS / "ddr.xml", encoding="utf-8", xml_declaration=True)
    return totals


def run(args) -> int:
    REPORTS.mkdir(parents=True, exist_ok=True)
    BUILD.mkdir(parents=True, exist_ok=True)
    manifest_path = REPORTS / "manifest.json"
    manifest_path.unlink(missing_ok=True)
    for stale in REPORTS.glob("*.xml"):
        stale.unlink()
    manifest = dict(schema=1, status="running", started_utc=datetime.now(timezone.utc).isoformat(),
                    mode="simulation-only" if args.simulation_only else "full-gate",
                    python=sys.version, platform=platform.platform(), seed=args.seed,
                    commands=[], runs=[], errors=[], artifacts=[])
    suites = []
    before = None
    identity = None
    started = time.monotonic()
    try:
        identity = git_identity()
        before = sources()
        manifest["project"] = identity
        manifest["source_sha256_before"] = before
        config = provenance.load_config()
        manifest["ordinary_dependencies"] = provenance.verify_python_version_pins(config["_version_pins"])
        checkouts = {}
        manifest["source_checkouts"] = {}
        for name in ("litepcie", "litenvme"):
            explicit = getattr(args, name + "_source")
            expected = config["repositories"][name]
            if explicit:
                verified = provenance.verify_checkout(explicit, expected["commit"])
            else:
                destination = BUILD / "dependencies" / f"{name}-{expected['commit']}"
                verified = provenance._fetch_checkout(name, destination, expected, REPORTS, manifest)
            checkouts[name] = Path(verified["path"])
            manifest["source_checkouts"][name] = verified
        env = provenance.build_python_env(checkouts)
        origins = provenance._read_import_origins(checkouts, env, REPORTS, manifest)
        manifest["dependencies"] = provenance.verify_python_provenance(
            config, source_paths=checkouts, import_origins=origins)
        for tool in ("iverilog", "vvp") + (() if args.simulation_only else ("yosys",)):
            executable = shutil.which(tool)
            if not executable:
                raise RuntimeError(f"required tool missing: {tool}")
            step("version-" + tool, [executable, "-V"], manifest, timeout=15)
        if not EXPECTED_TESTS:
            raise RuntimeError("independent DDR test contract has not been frozen")
        if set(PROFILES) != set(EXPECTED_PROFILES):
            raise RuntimeError(f"DDR profile matrix changed: {list(PROFILES)}")
        for index, (profile, parameters) in enumerate(PROFILES.items()):
            build = BUILD / profile
            build.mkdir(parents=True, exist_ok=True)
            generated = build / "hostmem_ddr_fixture.v"
            waveform = build / "ddr_tb.fst"
            generated.unlink(missing_ok=True)
            waveform.unlink(missing_ok=True)
            record = dict(profile=profile, parameters=parameters, seed=args.seed + index * 101)
            manifest["runs"].append(record)
            step("generate-" + profile,
                 [sys.executable, "gateware/hostmem_ddr_fixture.py", "--output", str(generated),
                  "--data-width", str(parameters["DATA_WIDTH"]), "--base", str(PCIE_BASE),
                  "--size", str(parameters["APERTURE_BYTES"])], manifest, env=env, timeout=60)
            record["generated_rtl"] = dict(path=str(generated.relative_to(ROOT)), sha256=sha(generated))
            try:
                step("simulate-" + profile,
                     [sys.executable, str(Path(__file__).resolve()), "--worker", profile,
                      "--generated", str(generated), "--seed", str(record["seed"])],
                     manifest, env=env, timeout=180)
            finally:
                junit = REPORTS / f"{profile}.xml"
                if junit.is_file():
                    suites.append(ET.parse(junit).getroot())
            summary = provenance.validate_junit(junit)
            names = [case.get("name") for case in ET.parse(junit).getroot().iter("testcase")]
            if sorted(names) != sorted(EXPECTED_TESTS):
                raise RuntimeError(f"{profile}: unexpected test names: {names}")
            if not waveform.is_file() or waveform.stat().st_size == 0:
                raise RuntimeError(f"{profile}: missing fresh waveform")
            record["result"] = summary
            record["waveform"] = dict(path=str(waveform.relative_to(ROOT)), sha256=sha(waveform))
        if args.simulation_only:
            manifest["synthesis"] = dict(status="not_run", reason="--simulation-only")
        else:
            output = REPORTS / "capture_ddr_writer.json"
            output.unlink(missing_ok=True)
            script = "; ".join(["read_verilog -sv rtl/memory/capture_ddr_writer.sv",
                                "hierarchy -check -top capture_ddr_writer", "proc", "opt",
                                "memory_collect", "opt_clean", "check -assert",
                                "select -assert-none t:$dlatch t:$adlatch", "stat",
                                f"write_json {output}"])
            step("yosys-writer", ["yosys", "-p", script], manifest, timeout=120)
            if not output.is_file():
                raise RuntimeError("synthesis produced no netlist")
            manifest["synthesis"] = dict(status="passed", netlist_sha256=sha(output))
        for name, path in checkouts.items():
            provenance.verify_checkout(path, config["repositories"][name]["commit"])
    except (Exception, SystemExit) as exc:
        manifest["errors"].append(f"{type(exc).__name__}: {exc}")
    finally:
        try:
            after = sources()
            manifest["source_sha256_after"] = after
            if before != after:
                raise RuntimeError("qualification sources changed during the run")
            final_identity = git_identity()
            if not identity or any(final_identity[key] != identity[key] for key in ("head", "origin")):
                raise RuntimeError("project identity changed during the run")
        except Exception as exc:
            manifest["errors"].append(str(exc))
        manifest["junit_summary"] = aggregate(suites, manifest["errors"])
        manifest["status"] = "failed" if manifest["errors"] else (
            "simulation_passed" if args.simulation_only else "passed")
        manifest["elapsed_seconds"] = round(time.monotonic() - started, 3)
        manifest["finished_utc"] = datetime.now(timezone.utc).isoformat()
        # Only enumerate this run's outputs. Old debug logs or unexecuted
        # profile waveforms must not be represented as current evidence.
        artifacts = {REPORTS / "ddr.xml"}
        if not args.simulation_only:
            artifacts.add(REPORTS / "capture_ddr_writer.json")
        for command in manifest["commands"]:
            for key in ("stdout", "stderr"):
                if command.get(key):
                    candidate = Path(command[key])
                    artifacts.add(candidate if candidate.is_absolute() else ROOT / candidate)
        for record in manifest["runs"]:
            artifacts.update((REPORTS / f"{record['profile']}.xml",
                              BUILD / record["profile"] / "hostmem_ddr_fixture.v",
                              BUILD / record["profile"] / "ddr_tb.fst"))
        for path in sorted(artifacts):
            if path.is_file():
                manifest["artifacts"].append(dict(path=str(path.relative_to(ROOT)),
                                                   size_bytes=path.stat().st_size, sha256=sha(path)))
        manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    print(json.dumps(dict(status=manifest["status"], result=manifest["junit_summary"],
                         errors=manifest["errors"]), indent=2))
    return int(bool(manifest["errors"]))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--simulation-only", action="store_true")
    parser.add_argument("--litepcie-source", type=Path)
    parser.add_argument("--litenvme-source", type=Path)
    parser.add_argument("--seed", type=int, default=20261021)
    parser.add_argument("--worker", choices=list(PROFILES), help=argparse.SUPPRESS)
    parser.add_argument("--generated", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.worker:
        if not args.generated:
            parser.error("worker requires generated RTL")
        worker(args.worker, args.generated, args.seed)
        return 0
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
