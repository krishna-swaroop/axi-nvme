"""Independent queue scoreboard with stalls and reset during traffic."""
from collections import deque
import random
import cocotb
from cocotb.triggers import Timer


@cocotb.test()
async def random_stalls_and_reset(dut):
    expected = deque()
    pending = None
    held = None
    sent = received = 0
    dut.clk.value = 0
    for cycle in range(4000):
        reset = cycle < 3 or cycle in (731, 732)
        dut.clk.value = 0
        dut.rst.value = int(reset)
        if reset:
            pending = None
            expected.clear()
            held = None
        elif pending is None and cycle < 3000 and random.random() < 0.85:
            pending = random.getrandbits(32)
        dut.s_valid.value = int(pending is not None)
        dut.s_data.value = pending or 0
        dut.m_ready.value = int(cycle >= 3000 or random.random() < 0.55)
        await Timer(4, unit="ns")
        if not reset:
            valid = int(dut.m_valid.value)
            value = int(dut.m_data.value)
            if held is not None:
                assert valid and value == held, "Payload changed while stalled"
            if valid and int(dut.m_ready.value):
                assert expected, "Unexpected output"
                assert value == expected.popleft(), "Reordered or corrupted payload"
                received += 1
            held = value if valid and not int(dut.m_ready.value) else None
            if pending is not None and int(dut.s_ready.value):
                expected.append(pending)
                sent += 1
                pending = None
        dut.clk.value = 1
        await Timer(4, unit="ns")
        if reset:
            assert int(dut.m_valid.value) == 0, "Reset failed to clear validity"
    assert not expected and pending is None, "Did not drain"
    assert sent > 1000 and received > 1000, "Insufficient traffic exercised"
