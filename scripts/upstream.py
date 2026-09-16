"""Fetch one immutable LiteNVMe checkout and run its unmodified test suite."""
import json
from pathlib import Path
import subprocess
import sys

from check import ROOT, REPORTS, run


if __name__ == "__main__":
    cfg = json.loads((ROOT / "config/upstream.json").read_text())
    dest = ROOT / ".deps" / "litenvme"
    if not dest.exists():
        dest.mkdir(parents=True)
        run("upstream-init", ["git", "init", str(dest)])
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=dest,
                          capture_output=True, text=True)
    if head.returncode == 0 and head.stdout.strip() != cfg["commit"]:
        raise SystemExit("Existing upstream checkout has a different SHA; inspect it manually")
    if head.returncode != 0:
        run("upstream-fetch", ["git", "fetch", "--depth", "1", cfg["repository"], cfg["commit"]], cwd=dest)
        run("upstream-checkout", ["git", "checkout", "--detach", cfg["commit"]], cwd=dest)
    actual = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=dest, text=True).strip()
    dirty = subprocess.check_output(["git", "status", "--porcelain", "--untracked-files=no"], cwd=dest, text=True)
    if actual != cfg["commit"] or dirty:
        raise SystemExit("Upstream revision or tracked content mismatch")
    run("upstream-test", [sys.executable, "-m", "pytest", "-q", "--tb=short",
                          f"--junitxml={REPORTS / 'upstream.xml'}"], cwd=dest, timeout=600)
