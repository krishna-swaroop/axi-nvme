"""Fail-closed local check runner, with bounded subprocesses and artifact capture."""
import json
import os
from pathlib import Path
import platform
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
REPORTS = ROOT / "reports"


def run(name, args, cwd=ROOT, timeout=180):
    REPORTS.mkdir(exist_ok=True)
    log = REPORTS / f"{name}.log"
    with log.open("w") as out:
        try:
            result = subprocess.run(args, cwd=cwd, stdout=out, stderr=subprocess.STDOUT,
                                    timeout=timeout, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            out.write(str(exc))
            raise SystemExit(f"FAIL {name}: {log}") from exc
    print(log.read_text()[-5000:])
    if result.returncode:
        raise SystemExit(f"FAIL {name} ({result.returncode}): {log}")
    return log


if __name__ == "__main__":
    REPORTS.mkdir(exist_ok=True)
    for tool in ("iverilog", "vvp"):
        run(tool, [tool, "-V"])
    versions = {"python": sys.version, "platform": platform.platform(),
                "seed": os.environ.get("COCOTB_RANDOM_SEED", "20260917")}
    revision = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT,
                              capture_output=True, text=True)
    versions["git_revision"] = revision.stdout.strip() if revision.returncode == 0 else "uncommitted-bootstrap"
    versions["git_dirty"] = bool(subprocess.check_output(
        ["git", "status", "--porcelain"], cwd=ROOT, text=True).strip())
    versions["dependencies"] = subprocess.check_output(
        [sys.executable, "-m", "pip", "freeze"], text=True)
    (REPORTS / "environment.json").write_text(json.dumps(versions, indent=2))
    run("unit", [sys.executable, "-m", "pytest", "-q", "tests/test_budget.py", "tests/test_qualification_runner.py", "tests/test_storage_oracle.py",
                 f"--junitxml={REPORTS / 'unit.xml'}"])
    run("rtl-smoke", [sys.executable, "scripts/sim.py"])
    print("PASS: planning/provenance tests and RTL harness canary; NVMe qualification runs separately")
