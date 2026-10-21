# Bootstrap validation evidence

Date: 2026-09-17. Host: macOS Apple Silicon, Python 3.12, Icarus Verilog 12.0,
cocotb 2.0.1. Dependency revisions are in `requirements.lock`.

| Check | Result | Meaning |
|---|---|---|
| `make check` planning tests | 7 passed | Unit conversions, buffer/recovery arithmetic and invalid assumptions |
| Icarus/cocotb harness | 1 passed, 0 failed/skipped; 4,000 cycles, 32 us simulated | Independent queue scoreboard, random stalls and reset; fixture only |
| Waveform | `build/smoke/axis_canary.fst` generated | Feedback available to agents for debugging |
| `make budget` | 125 MB backlog / 100 ms at 10 Gbit/s; 156.25 MB with 25% margin | Planning scenario, not measured SSD behavior |
| LiteNVMe full regression | Pending at bootstrap; revalidated below | See the 2026-10-21 record |
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

## 2026-10-21 development checkpoint

Restored the archived workspace, including the previously unpublished stream-ring
integration work. The original architecture edit and board template were preserved;
the development PR is isolated from those pre-existing files.

| Check executed on this Mac | Result |
|---|---|
| `make check` | 23 planning/provenance tests and 1 Icarus harness test passed |
| `make upstream-test` | 46 passed, no failures/skips, 261.17 seconds |
| PCIe targeted transport regression | 45 passed, no failures/skips |
| Generated stream DMA in Icarus | 4 passed: 64, 128, 256 and 512 bits |
| New failed-read tests | 6 passed after 5 original stale-data failures were reproduced |
| New AXI transfer-size tests | 5 passed after 4 original failures were reproduced |
| Pattern abort/restart and integration | 3 passed, including idle-checker regression; existing pattern roundtrip also passed |
| Initial combined local qualification | Failed at the 20-minute wall-clock limit in the whole-core test; 79/81 NVMe cases completed without an observed failure, but no aggregate NVMe JUnit pass was produced |
| Clean Linux qualification | Required on the development PR; use its checks and retained artifacts for the final result |

The development pins include the restored stream-ring integration plus reviewed
AXI-size/read-error/checker-abort fixes. `config/development.json` is the canonical
source list. `reports/development/manifest.json` records effective import paths,
the installed generator dependency revisions, command results and JUnit counts.
See [the qualification scope](qualification/STREAM_RING.md) for remaining gates.
The initial full development qualification budget is 90 minutes, including the
slow whole-core Migen simulation. Named test progress and slow-test
durations are retained to diagnose long Migen simulations. A timeout remains a
failure and never counts as partially passing qualification.

The local run recorded a modified project checkout. The clean commit-bound
qualification and its evidence are published through
[the development PR](https://github.com/krishna-swaroop/axi-nvme/pull/1).

No Vivado synthesis, implementation, timing closure, hardware programming or SSD
writes were performed in this checkpoint. The 10 Gbit/s capture goal remains
conditional on PL DDR, non-pausable capture handling and measured SSD service.
