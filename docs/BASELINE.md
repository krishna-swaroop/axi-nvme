# Bootstrap validation evidence

Date: 2026-09-17. Host: macOS Apple Silicon, Python 3.12, Icarus Verilog 12.0,
cocotb 2.0.1. Dependency revisions are in `requirements.lock`.

| Check | Result | Meaning |
|---|---|---|
| `make check` planning tests | 7 passed | Unit conversions, buffer/recovery arithmetic and invalid assumptions |
| Icarus/cocotb harness | 1 passed, 0 failed/skipped; 4,000 cycles, 32 us simulated | Independent queue scoreboard, random stalls and reset; fixture only |
| Waveform | `build/smoke/axis_canary.fst` generated | Feedback available to agents for debugging |
| `make budget` | 125 MB backlog / 100 ms at 10 Gbit/s; 156.25 MB with 25% margin | Planning scenario, not measured SSD behavior |
| LiteNVMe full regression | Qualification run pending | See final result update before relying on upstream |
| Device synthesis / timing / hardware | Not run | Vivado, exact board and lab qualification remain future gates |

The first upstream trial using GitHub Migen mirror HEAD
`e19524c963a8342952840983047557707fbe0b6a` produced 23 passes and 23 failures,
including `MemoryToArray` attempting `.eq` on a missing read port. A historical
LiteX pin did not resolve it. LiteX's own `litex_repos.py` specifies Migen
`4c2ae8dfeea37f235b52acb8166f12acaaae4f7c` from `git.m-labs.hk/M-Labs/migen`;
the lock now follows that dependency declaration with current pinned LiteX.
No upstream RTL/test edits or skipped failures were used to hide the initial error.

Reproduce with a fresh virtual environment; do not trust a package's version
string alone. Pip can retain a same-version VCS wheel when changing its commit;
use a clean venv or explicit `--force-reinstall --no-deps` for a reviewed replacement.
The environment manifest records actual installed VCS commit IDs via pip freeze.

Local generated logs/JUnit are in ignored `reports/`; CI uploads evidence on
both pass and failure. This document records outcomes, not an NVMe performance
claim. Upstream's KU3P hardware benchmark remains external, unreproduced evidence.
