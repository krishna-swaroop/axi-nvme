# Third-party provenance

No research candidate source is vendored into original project directories.
Dependencies are fetched into the ignored `.deps/` or Python virtual environment.
Keep original copyright and license notices if source is later redistributed.

| Source | Inspected revision / role | License/disposition |
|---|---|---|
| enjoy-digital/litenvme | ce9c478c3df794685e50bab503ab4a7764b6f983 | BSD-2-Clause; evaluation dependency |
| enjoy-digital/litepcie | c4ac65f3340f7ded11470ebec165ae06c563def6 | BSD-2-Clause; evaluation dependency |
| enjoy-digital/litex | See requirements.lock | BSD-2-Clause; generator dependency |
| krishna-swaroop/litenvme | See config/development.json | BSD-2-Clause; stream-ring development fork of enjoy-digital/litenvme |
| krishna-swaroop/litepcie | See config/development.json | BSD-2-Clause; Root Port configuration-write completion fix |
| M-Labs/migen | See requirements.lock | BSD-2-Clause; generator/simulation dependency |
| DUNE/pl-nvme | 971d95f5c89ad6e9960fae62d51cd82545856706 | GPL-3.0-or-later in inspected VHDL; research only |
| yhqiu16/NVMeCHA | f9a9a2e6ab2dc75650d154e17f62ab72b461742b | GPL-3.0 repository; endpoint research only |
| mcrl/NVMe | 789c8caf419fa70a9a1a5041f009e1b17206b8a3 | Three-clause BSD-style top-level notice; per-file audit needed before import |

Simulator/tool licenses do not automatically determine the license of independent
RTL being simulated. Conversely, copying GPL RTL is not made BSD by placing it in
this repository. Review the actual files and resulting distribution before reuse.
AMD IP/tool licenses apply to their own components. The project does not redistribute
NVMe/PCIe specification documents; reference the
[official specifications](https://nvmexpress.org/specifications/) and
[archives](https://nvmexpress.org/nvm-express-specification-archives/) instead.
