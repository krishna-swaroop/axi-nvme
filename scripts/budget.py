"""Planning arithmetic; decimal link rates, binary memory capacities."""
import json
import math
from pathlib import Path


def calculate(input_gbps, stall_ms, margin, pl_buffer_mib, ssd_service_gbps, **_):
    values = (input_gbps, stall_ms, margin, pl_buffer_mib, ssd_service_gbps)
    if not all(math.isfinite(v) for v in values):
        raise ValueError("budget values must be finite")
    if min(input_gbps, pl_buffer_mib, ssd_service_gbps) <= 0 or stall_ms < 0 or margin < 1:
        raise ValueError("rates/capacity > 0, stall >= 0 and margin >= 1 required")
    rate = input_gbps * 1e9 / 8
    backlog = rate * stall_ms / 1000
    spare = (ssd_service_gbps - input_gbps) * 1e9 / 8
    return {
        "input_bytes_per_second": rate,
        "stall_backlog_bytes": backlog,
        "required_bytes_with_margin": math.ceil(backlog * margin),
        "buffer_bytes": pl_buffer_mib * 2**20,
        "zero_service_coverage_ms_before_reserves": pl_buffer_mib * 2**20 / rate * 1000,
        "recovery_ms_at_assumed_service": backlog / spare * 1000 if spare > 0 else None,
        "sustained_service_exceeds_input": spare > 0,
        "scenario_fits_before_reserves": pl_buffer_mib * 2**20 >= backlog * margin,
    }


if __name__ == "__main__":
    root = Path(__file__).resolve().parents[1]
    print(json.dumps(calculate(**json.loads((root / "config/capture.json").read_text())), indent=2))
