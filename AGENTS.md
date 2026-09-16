# Agent working contract

Read README.md, docs/ARCHITECTURE.md, docs/ROADMAP.md and docs/BASELINE.md first.
This is an NVMe **host**, not an SSD-side NVMe endpoint. The camera cannot pause.

- Work one bounded ROADMAP package per branch/PR, declaring its file allowlist,
  dependencies, observable behavior, verification commands and stop conditions.
- Preserve CPU-free steady-state operation. No hidden PS DDR, Linux service or
  per-I/O firmware requirement. PL DDR is permitted and expected for capture.
- Use synthesizable SystemVerilog for new portable modules; preserve upstream
  Migen source if evaluating/modifying LiteNVMe. Never hand-edit generated RTL.
- Keep the PCIe/DDR vendor shell separate from portable logic and test models.
- Never invent pins, hard-IP sites, clocks, SSD capabilities or timing results.
  ZCU102/ZU9EG has no PL PCIe hard block; board integration is gated accordingly.
- Reproduce a regression before changing logic; add an independent assertion or
  scoreboard derived from the contract. Do not weaken tests to manufacture green.
- Pin upstream SHAs and Python dependencies. Record simulator versions and seed.
  Review dependency changes separately from behavioral changes where possible.
- Run `make check`; changes touching the upstream integration also require
  `make upstream-test`. Missing tools are failures, not skipped passing checks.
- Before merge, perform a separate review of source, test independence, buffer
  ownership, resets, clock crossings, failure paths, and claimed evidence.
  The author cannot call a self-review an independent review. An independent
  reviewer pass can be scheduled when implementation PRs begin.
- Do not claim simulation proves hardware timing, SSD tail latency, or durability.
- SSD hardware writes require an explicitly selected disposable LBA range and
  drive identity. Do not format, sanitize or overwrite arbitrary storage.
- Public CI is unprivileged. Never run untrusted PR code on the hardware runner
  or expose its SSH/JTAG credentials. See docs/AGENT_WORKFLOW.md.
- Stop and report scope changes if a task needs a different transport, a new
  license obligation, or contradicts an interface contract.
