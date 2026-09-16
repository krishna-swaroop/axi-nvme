import pytest
from scripts.budget import calculate


def test_10gbps_100ms_and_recovery():
    result = calculate(10, 100, 1.25, 256, 14)
    assert result["stall_backlog_bytes"] == 125_000_000
    assert result["required_bytes_with_margin"] == 156_250_000
    assert result["recovery_ms_at_assumed_service"] == 250
    assert result["scenario_fits_before_reserves"]


@pytest.mark.parametrize("service", [8, 10])
def test_no_recovery_at_insufficient_service(service):
    result = calculate(10, 100, 1, 1, service)
    assert result["recovery_ms_at_assumed_service"] is None
    assert not result["sustained_service_exceeds_input"]
    assert not result["scenario_fits_before_reserves"]


@pytest.mark.parametrize("rate,stall,margin", [(0, 1, 1), (10, -1, 1), (10, 1, .5), (float("nan"), 1, 1)])
def test_invalid_assumptions(rate, stall, margin):
    with pytest.raises(ValueError):
        calculate(rate, stall, margin, 256, 14)
