"""Contract-level tests with an independent chunk scoreboard and byte ledger."""

from collections import deque
import os
import random

import cocotb
from cocotb.triggers import Timer


class StagerBench:
    def __init__(self, dut):
        self.dut = dut
        self.data_width = len(dut.s_data)
        self.beat_bytes = self.data_width // 8
        self.chunk_beats = int(os.environ["CAPTURE_CHUNK_BEATS"])
        self.banks = int(os.environ["CAPTURE_BANKS"])
        self.counter_width = int(os.environ["CAPTURE_COUNTER_WIDTH"])
        self.sequence_width = int(os.environ["CAPTURE_SEQUENCE_WIDTH"])
        self.counter_max = (1 << self.counter_width) - 1
        self.expected = deque()
        self.held = None
        self.input_total = 0
        self.output_total = 0
        self.dropped_total = 0
        self.unarmed_total = 0
        self.overflow_total = 0
        self.aborted_total = 0
        self.buffered_total = 0
        self.overflow_seen = False
        self.counter_saturated_seen = False
        self.require_continuous_output = False

    def expect_chunk(self, sequence, payload):
        assert len(payload) == self.chunk_beats
        for index, value in enumerate(payload):
            self.expected.append((value, int(index == self.chunk_beats - 1), sequence))

    def _counter_value(self, total):
        return min(total, self.counter_max)

    def _check_counters(self):
        dut = self.dut
        expected = {
            "input_bytes": self.input_total,
            "output_bytes": self.output_total,
            "dropped_bytes": self.dropped_total,
            "unarmed_bytes": self.unarmed_total,
            "overflow_chunks": self.overflow_total,
            "aborted_chunks": self.aborted_total,
        }
        for name, total in expected.items():
            assert int(getattr(dut, name).value) == self._counter_value(total), (
                f"{name}: got {int(getattr(dut, name).value)}, expected "
                f"{self._counter_value(total)} from {total} events"
            )
        assert int(dut.buffered_bytes.value) == self.buffered_total, (
            f"buffered_bytes={int(dut.buffered_bytes.value)}, expected {self.buffered_total}"
        )
        assert int(dut.overflow_sticky.value) == int(self.overflow_seen)
        assert int(dut.counter_saturated.value) == int(self.counter_saturated_seen)
        if not self.counter_saturated_seen:
            assert self.input_total == self.output_total + self.dropped_total + self.buffered_total, (
                "byte conservation failed before counter saturation"
            )

    def _add_counter(self, total, amount):
        if total + amount > self.counter_max:
            self.counter_saturated_seen = True
        return total + amount

    async def cycle(
        self,
        *,
        arm=0,
        valid=0,
        data=0,
        ready=0,
        rst=0,
        input_class=None,
        overflow_event=False,
        abort_bytes=0,
        aborted_event=False,
        after_chunk=None,
        settle=None,
    ):
        dut = self.dut
        dut.clk.value = 0
        dut.rst.value = rst
        dut.arm.value = arm
        dut.s_valid.value = valid
        dut.s_data.value = data
        dut.m_ready.value = ready
        await Timer(2, unit="ns")

        # Registered outputs may be X before the first synchronous reset edge.
        # Reset cycles invalidate model state, so only sample outputs afterward.
        if rst:
            pre = {"valid": 0, "ready": int(dut.m_ready.value), "data": 0,
                   "last": 0, "sequence": 0}
        else:
            pre = {
                "valid": int(dut.m_valid.value),
                "ready": int(dut.m_ready.value),
                "data": int(dut.m_data.value),
                "last": int(dut.m_last.value),
                "sequence": int(dut.m_sequence.value),
            }
        if self.require_continuous_output and self.expected:
            assert pre["valid"], "always-ready output inserted a bubble before a queued beat"
        if settle is not None:
            override = settle(pre) or {}
            arm = override.get("arm", arm)
            valid = override.get("valid", valid)
            data = override.get("data", data)
            input_class = override.get("input_class", input_class)
            overflow_event = override.get("overflow_event", overflow_event)
            after_chunk = override.get("after_chunk", after_chunk)
            dut.arm.value = arm
            dut.s_valid.value = valid
            dut.s_data.value = data
            await Timer(1, unit="ns")

        actual_arm = int(dut.arm.value)
        actual_valid = int(dut.s_valid.value)
        actual_ready = int(dut.s_ready.value)
        assert actual_ready == (0 if rst else 1), "input was backpressured"

        if rst:
            self.expected.clear()
            self.held = None
            self.input_total = 0
            self.output_total = 0
            self.dropped_total = 0
            self.unarmed_total = 0
            self.overflow_total = 0
            self.aborted_total = 0
            self.buffered_total = 0
            self.overflow_seen = False
            self.counter_saturated_seen = False
        else:
            if self.held is not None:
                assert pre["valid"] and (
                    pre["data"], pre["last"], pre["sequence"]
                ) == self.held, "valid/data/last/sequence changed while output was stalled"

            if pre["valid"] and pre["ready"]:
                assert self.expected, "unexpected output beat (partial or rejected chunk leaked)"
                expected = self.expected.popleft()
                actual = (pre["data"], pre["last"], pre["sequence"])
                assert actual == expected, f"output mismatch: got {actual}, expected {expected}"
                self.output_total = self._add_counter(self.output_total, self.beat_bytes)
                self.buffered_total -= self.beat_bytes
                assert self.buffered_total >= 0, "output consumed unbuffered data"
                self.held = None
            elif pre["valid"]:
                self.held = (pre["data"], pre["last"], pre["sequence"])
            else:
                self.held = None

            if actual_valid:
                if actual_arm:
                    assert input_class in ("store", "drop"), "test must classify each armed input beat"
                    self.input_total = self._add_counter(self.input_total, self.beat_bytes)
                    if input_class == "store":
                        self.buffered_total += self.beat_bytes
                    else:
                        self.dropped_total = self._add_counter(self.dropped_total, self.beat_bytes)
                else:
                    self.unarmed_total = self._add_counter(self.unarmed_total, self.beat_bytes)

            if overflow_event:
                self.overflow_total = self._add_counter(self.overflow_total, 1)
                self.overflow_seen = True
            if abort_bytes:
                self.dropped_total = self._add_counter(self.dropped_total, abort_bytes)
                self.buffered_total -= abort_bytes
                assert self.buffered_total >= 0, "abort discarded unbuffered data"
            if aborted_event:
                self.aborted_total = self._add_counter(self.aborted_total, 1)

        await Timer(1, unit="ns")
        dut.clk.value = 1
        await Timer(2, unit="ns")
        if rst:
            assert int(dut.m_valid.value) == 0, "reset did not invalidate output"
        else:
            if self.require_continuous_output and self.expected:
                assert int(dut.m_valid.value), "always-ready output inserted a bubble between queued beats"
            if after_chunk is not None:
                self.expect_chunk(*after_chunk)
            self._check_counters()
        dut.clk.value = 0
        await Timer(1, unit="ns")
        return pre

    async def reset(self):
        for _ in range(2):
            await self.cycle(rst=1)
        await self.cycle()

    def random_payload(self, rng, token):
        mask = (1 << self.data_width) - 1
        # Mix a chunk identity and beat index into random data so adjacent chunks
        # remain distinguishable even at the 8-bit test width.
        return [((rng.getrandbits(self.data_width) ^ (token * 0x9D) ^ (i * 0x35)) & mask)
                for i in range(self.chunk_beats)]

    async def send_chunk(self, payload, sequence, *, admitted, ready, gaps=False):
        for index, value in enumerate(payload):
            if gaps and index != 0 and index % 2:
                await self.cycle(arm=1, ready=ready)
            final = index == self.chunk_beats - 1
            await self.cycle(
                arm=1,
                valid=1,
                data=value,
                ready=ready,
                input_class="store" if admitted else "drop",
                overflow_event=(not admitted and index == 0),
                after_chunk=(sequence, payload) if admitted and final else None,
            )

    async def drain(self, limit=None):
        if limit is None:
            limit = self.banks * self.chunk_beats * 4 + 32
        for _ in range(limit):
            await self.cycle(arm=1, ready=1)
            if not self.expected and not int(self.dut.m_valid.value):
                return
        assert False, "output did not drain within the bounded test interval"


def _seed(offset=0):
    return int(os.environ.get("COCOTB_RANDOM_SEED", "20261021")) + offset


@cocotb.test(timeout_time=2, timeout_unit="ms")
async def test_full_rate_sustained(dut):
    tb = StagerBench(dut)
    await tb.reset()
    rng = random.Random(_seed(1))
    chunks = max(6, tb.banks * 2)
    for token in range(chunks):
        await tb.send_chunk(tb.random_payload(rng, token), token, admitted=True, ready=1)
    assert int(dut.m_valid.value), "completed chunks never reached the output register"
    # All input chunks are sealed now, so every expected beat belongs to a queued bank.
    # Require valid on every cycle while the always-ready sink drains those banks.
    tb.require_continuous_output = True
    await tb.drain()
    assert tb.input_total == chunks * tb.chunk_beats * tb.beat_bytes
    assert tb.output_total == tb.input_total
    assert tb.dropped_total == 0 and tb.buffered_total == 0
    assert int(dut.overflow_chunks.value) == 0
    assert int(dut.sequence_exhausted.value) == 0


@cocotb.test(timeout_time=2, timeout_unit="ms")
async def test_chunk_drop_order_and_release_admission(dut):
    tb = StagerBench(dut)
    await tb.reset()
    rng = random.Random(_seed(2))
    chunk_payloads = [tb.random_payload(rng, token) for token in range(tb.banks + 3)]

    # Keep output stalled until every bank is occupied. For multi-beat chunks, prove
    # that a partial bank is not published before its final valid input beat.
    if tb.chunk_beats > 1:
        payload = chunk_payloads[0]
        for index, value in enumerate(payload[:-1]):
            if index % 2:
                await tb.cycle(arm=1, ready=0)
            await tb.cycle(arm=1, valid=1, data=value, ready=0, input_class="store")
            assert int(dut.m_valid.value) == 0, "partial chunk became visible"
        await tb.cycle(
            arm=1, valid=1, data=payload[-1], ready=0, input_class="store",
            after_chunk=(0, payload),
        )
        next_sequence = 1
    else:
        await tb.send_chunk(chunk_payloads[0], 0, admitted=True, ready=0, gaps=True)
        next_sequence = 1

    for bank in range(1, tb.banks):
        await tb.send_chunk(chunk_payloads[bank], next_sequence, admitted=True, ready=0, gaps=True)
        next_sequence += 1

    for index in range(2):
        await tb.send_chunk(
            chunk_payloads[tb.banks + index], next_sequence, admitted=False, ready=0, gaps=True,
        )
        next_sequence += 1

    expected_occupancy = tb.banks * tb.chunk_beats * tb.beat_bytes
    assert tb.buffered_total == expected_occupancy
    assert int(dut.buffered_bytes.value) == expected_occupancy
    assert int(dut.overflow_chunks.value) == 2
    assert int(dut.overflow_sticky.value) == 1

    # On the edge accepting the oldest chunk's final beat, begin a new capture. Its
    # first write reuses that just-released bank; queued chunks continue draining.
    reuse_payload = chunk_payloads[-1]
    reuse_sequence = tb.banks + 2
    sent_first = False

    def start_on_release(pre):
        nonlocal sent_first
        if not sent_first and pre["valid"] and pre["last"]:
            sent_first = True
            return {
                "arm": 1,
                "valid": 1,
                "data": reuse_payload[0],
                "input_class": "store",
                "after_chunk": ((reuse_sequence, reuse_payload)
                                if tb.chunk_beats == 1 else None),
            }
        return {"arm": 1, "valid": 0}

    for _ in range(tb.banks * tb.chunk_beats * 2 + 16):
        await tb.cycle(arm=1, ready=1, settle=start_on_release)
        if sent_first:
            break
    assert sent_first, "did not observe final output handshake for same-edge admission"
    for index, value in enumerate(reuse_payload[1:], start=1):
        await tb.cycle(
            arm=1, valid=1, data=value, ready=1, input_class="store",
            after_chunk=(reuse_sequence, reuse_payload)
            if index == tb.chunk_beats - 1 else None,
        )

    await tb.drain()
    assert int(dut.overflow_chunks.value) == 2
    assert int(dut.overflow_sticky.value) == 1
    assert tb.dropped_total == 2 * tb.chunk_beats * tb.beat_bytes
    assert tb.input_total == tb.output_total + tb.dropped_total
    assert tb.output_total == (tb.banks + 1) * tb.chunk_beats * tb.beat_bytes
    assert tb.buffered_total == 0


@cocotb.test(timeout_time=2, timeout_unit="ms")
async def test_partial_disarm_and_rearm(dut):
    tb = StagerBench(dut)
    await tb.reset()
    rng = random.Random(_seed(3))
    first = tb.random_payload(rng, 20)
    await tb.send_chunk(first, 0, admitted=True, ready=0)

    partial = []
    if tb.chunk_beats > 1:
        partial = tb.random_payload(rng, 21)[:min(tb.chunk_beats - 1, 7)]
        for value in partial:
            await tb.cycle(arm=1, valid=1, data=value, ready=0, input_class="store")
        before_disarm = (tb.chunk_beats + len(partial)) * tb.beat_bytes
        assert int(dut.buffered_bytes.value) == before_disarm
        await tb.cycle(
            arm=0, valid=1, data=0xA5, ready=0,
            abort_bytes=len(partial) * tb.beat_bytes,
            aborted_event=True,
        )
        assert int(dut.buffered_bytes.value) == tb.chunk_beats * tb.beat_bytes
    else:
        await tb.cycle(arm=0, valid=1, data=0xA5, ready=0)

    # A completed chunk remains valid and drains while capture is disarmed.
    for _ in range(tb.banks * tb.chunk_beats + 8):
        await tb.cycle(arm=0, ready=1)
        if not tb.expected:
            break
    else:
        assert False, "completed chunks did not drain while capture was disarmed"

    next_sequence = 2 if partial else 1
    next_payload = tb.random_payload(rng, 22)
    await tb.send_chunk(next_payload, next_sequence, admitted=True, ready=1, gaps=True)
    await tb.drain()
    assert tb.dropped_total == len(partial) * tb.beat_bytes
    assert int(dut.aborted_chunks.value) == int(bool(partial))
    assert tb.unarmed_total == tb.beat_bytes
    assert tb.buffered_total == 0


@cocotb.test(timeout_time=2, timeout_unit="ms")
async def test_disarm_rejected_partial_without_double_count(dut):
    tb = StagerBench(dut)
    await tb.reset()
    rng = random.Random(_seed(6))

    # Fill every bank, then begin a rejected chunk. Its received beats are already
    # included in dropped_bytes; disarming must clear the partial-drop state without
    # counting those bytes a second time.
    for sequence in range(tb.banks):
        payload = tb.random_payload(rng, 60 + sequence)
        await tb.send_chunk(payload, sequence, admitted=True, ready=0)

    partial_count = min(max(tb.chunk_beats - 1, 0), 3)
    if tb.chunk_beats == 1:
        dropped = tb.random_payload(rng, 70)
        await tb.send_chunk(dropped, tb.banks, admitted=False, ready=0)
        partial_count = 0
    else:
        dropped_payload = tb.random_payload(rng, 70)
        for index, value in enumerate(dropped_payload[:partial_count]):
            await tb.cycle(
                arm=1, valid=1, data=value, ready=0, input_class="drop",
                overflow_event=(index == 0),
            )

    await tb.cycle(arm=0, valid=1, data=0x5A, ready=0, aborted_event=bool(partial_count))
    await tb.drain()

    next_sequence = tb.banks + 1
    payload = tb.random_payload(rng, 71)
    await tb.send_chunk(payload, next_sequence, admitted=True, ready=1)
    await tb.drain()

    assert tb.dropped_total == partial_count * tb.beat_bytes + (
        tb.chunk_beats * tb.beat_bytes if tb.chunk_beats == 1 else 0
    )
    assert tb.unarmed_total == tb.beat_bytes
    assert int(dut.overflow_chunks.value) == 1
    assert int(dut.aborted_chunks.value) == int(bool(partial_count))
    assert tb.input_total == tb.output_total + tb.dropped_total
    assert tb.buffered_total == 0


@cocotb.test(timeout_time=2, timeout_unit="ms")
async def test_reset_while_output_stalled(dut):
    tb = StagerBench(dut)
    await tb.reset()
    rng = random.Random(_seed(4))
    payload = tb.random_payload(rng, 30)
    await tb.send_chunk(payload, 0, admitted=True, ready=0)
    for _ in range(tb.chunk_beats + 4):
        await tb.cycle(arm=1, ready=0)
        if int(dut.m_valid.value):
            break
    assert int(dut.m_valid.value) == 1, "completed chunk did not reach output register"
    assert int(dut.buffered_bytes.value) == tb.chunk_beats * tb.beat_bytes
    for _ in range(3):
        await tb.cycle(arm=1, ready=0)
    await tb.reset()
    assert int(dut.buffered_bytes.value) == 0
    assert int(dut.input_bytes.value) == 0
    payload_after_reset = tb.random_payload(rng, 31)
    await tb.send_chunk(payload_after_reset, 0, admitted=True, ready=1)
    await tb.drain()
    assert tb.output_total == tb.chunk_beats * tb.beat_bytes
    assert tb.buffered_total == 0


@cocotb.test(timeout_time=2, timeout_unit="ms")
async def test_randomized_sink_stalls(dut):
    tb = StagerBench(dut)
    await tb.reset()
    rng = random.Random(_seed(5))

    # Queue every bank while the sink is stalled, then include one full rejected
    # chunk. A seeded, irregular ready pattern checks output stability under varied
    # hold lengths while the independent scoreboard tracks ordering and occupancy.
    for sequence in range(tb.banks):
        payload = tb.random_payload(rng, 80 + sequence)
        await tb.send_chunk(payload, sequence, admitted=True, ready=0, gaps=True)
    dropped = tb.random_payload(rng, 90)
    await tb.send_chunk(dropped, tb.banks, admitted=False, ready=0, gaps=True)

    for _ in range(tb.banks * tb.chunk_beats * 20 + 64):
        await tb.cycle(arm=1, ready=int(rng.random() >= 0.42))
        if not tb.expected and not int(dut.m_valid.value):
            break
    else:
        assert False, "randomized sink stalls prevented bounded output drain"

    assert tb.dropped_total == tb.chunk_beats * tb.beat_bytes
    assert tb.output_total == tb.banks * tb.chunk_beats * tb.beat_bytes
    assert tb.input_total == tb.output_total + tb.dropped_total
    assert tb.buffered_total == 0
    assert int(dut.overflow_chunks.value) == 1
