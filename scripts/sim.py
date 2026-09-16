"""Compile/simulate the harness fixture; never includes production NVMe logic."""
import os
from pathlib import Path
import xml.etree.ElementTree as ET

from cocotb_tools.runner import get_runner

root = Path(__file__).resolve().parents[1]
reports = root / "reports"
reports.mkdir(exist_ok=True)
runner = get_runner("icarus")
runner.build(sources=[root / "tests/fixtures/axis_canary.sv"],
             hdl_toplevel="axis_canary", build_dir=root / "build/smoke",
             always=True, waves=True)
result = reports / "rtl-smoke.xml"
result.unlink(missing_ok=True)
runner.test(hdl_toplevel="axis_canary", test_module="test_axis_canary",
            test_dir=root / "tests/cocotb", results_xml=str(result), waves=True,
            seed=int(os.environ.get("COCOTB_RANDOM_SEED", "20260917")))
tree = ET.parse(result)
tests = list(tree.iter("testcase"))
if not tests or any(list(t.iter("failure")) or list(t.iter("error")) or list(t.iter("skipped")) for t in tests):
    raise SystemExit("RTL canary failed or did not execute")
