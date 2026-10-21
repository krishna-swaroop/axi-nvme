"""Small-width boundary checks, run only in the dedicated boundary profile."""

import random

import cocotb

from test_capture_stager import StagerBench, _seed


@cocotb.test(timeout_time=2, timeout_unit="ms")
async def test_sequence_exhaustion_and_counter_saturation(dut):
    tb = StagerBench(dut)
    assert tb.sequence_width == 2 and tb.counter_width == 4, "boundary profile not selected"
    await tb.reset()
    rng = random.Random(_seed(8))
    max_sequence = (1 << tb.sequence_width) - 1
    total_chunks = 20
    for sequence in range(total_chunks):
        payload = tb.random_payload(rng, 100 + sequence)
        await tb.send_chunk(
            payload,
            sequence,
            admitted=(sequence <= max_sequence),
            ready=1,
        )
    await tb.drain()
    assert int(dut.sequence_exhausted.value) == 1
    assert int(dut.overflow_sticky.value) == 1
    assert int(dut.counter_saturated.value) == 1
    assert int(dut.overflow_chunks.value) == tb.counter_max
    assert int(dut.input_bytes.value) == tb.counter_max
    assert int(dut.dropped_bytes.value) == tb.counter_max
    expected_output = (max_sequence + 1) * tb.chunk_beats * tb.beat_bytes
    assert tb.output_total == expected_output, "output ledger lost ownership after sequence exhaustion"
    assert int(dut.output_bytes.value) == min(expected_output, tb.counter_max)
    assert int(dut.buffered_bytes.value) == 0
