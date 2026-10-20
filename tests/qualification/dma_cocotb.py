"""Byte-addressed AXI RAM oracle with independent channels and delayed responses."""
from collections import deque
import random

import cocotb
from cocotb.triggers import Timer


class MemoryBus:
    def __init__(self, dut):
        self.dut = dut
        self.rng = random.Random(0x4E564D45 + len(dut.s_data))
        self.bytes_per_beat = len(dut.s_data) // 8
        self.memory = {}
        self.aw = deque()
        self.w = deque()
        self.b = deque()
        self.ar = deque()
        self.held_r = None
        self.cycles = 0
        self.write_count = self.read_count = self.responses = 0

    async def reset(self):
        d = self.dut
        for name in ("start", "we", "adr", "beats", "last_en", "s_valid", "s_data",
                     "m_ready", "axi_aw_ready", "axi_w_ready", "axi_b_valid", "axi_b_resp",
                     "axi_b_id", "axi_ar_ready", "axi_r_valid", "axi_r_data", "axi_r_resp",
                     "axi_r_id", "axi_r_last"):
            getattr(d, name).value = 0
        d.sys_clk.value = 0
        d.sys_rst.value = 1
        for _ in range(3):
            await self.tick(reset=True)
        d.sys_rst.value = 0
        await self.tick()

    def read(self, address):
        return sum(self.memory.get(address + i, 0) << (8 * i) for i in range(self.bytes_per_beat))

    async def tick(self, reset=False):
        d = self.dut
        d.sys_clk.value = 0
        self.cycles += 1
        aw_ready = not reset and self.rng.random() > 0.35
        w_ready = not reset and self.rng.random() > 0.45
        ar_ready = not reset and self.rng.random() > 0.35
        b_valid = bool(self.b and self.b[0] <= self.cycles)
        if self.held_r is None and self.ar and self.ar[0][0] <= self.cycles:
            _, address = self.ar.popleft()
            self.held_r = self.read(address)
        d.axi_aw_ready.value = int(aw_ready)
        d.axi_w_ready.value = int(w_ready)
        d.axi_b_valid.value = int(b_valid)
        d.axi_ar_ready.value = int(ar_ready)
        d.axi_r_valid.value = int(self.held_r is not None)
        d.axi_r_data.value = self.held_r or 0
        d.axi_r_last.value = 1
        await Timer(4, unit="ns")
        observed = {name: int(getattr(d, name).value) for name in
                    ("s_ready", "m_valid", "m_data", "m_last", "busy")}
        if not reset:
            if aw_ready and int(d.axi_aw_valid.value):
                assert int(d.axi_aw_len.value) == 0
                assert int(d.axi_aw_size.value) == self.bytes_per_beat.bit_length() - 1
                assert int(d.axi_aw_burst.value) == 1
                self.aw.append(int(d.axi_aw_addr.value))
            if w_ready and int(d.axi_w_valid.value):
                assert int(d.axi_w_last.value) == 1
                self.w.append((int(d.axi_w_data.value), int(d.axi_w_strb.value)))
            if b_valid and int(d.axi_b_ready.value):
                self.b.popleft()
                self.responses += 1
            if self.held_r is not None and int(d.axi_r_ready.value):
                self.held_r = None
                self.read_count += 1
            if ar_ready and int(d.axi_ar_valid.value):
                assert int(d.axi_ar_len.value) == 0
                assert int(d.axi_ar_size.value) == self.bytes_per_beat.bit_length() - 1
                assert int(d.axi_ar_burst.value) == 1
                self.ar.append((self.cycles + self.rng.randint(1, 9), int(d.axi_ar_addr.value)))
            if self.aw and self.w:
                address = self.aw.popleft()
                data, strobe = self.w.popleft()
                for i in range(self.bytes_per_beat):
                    if strobe & (1 << i):
                        self.memory[address + i] = (data >> (8 * i)) & 255
                self.write_count += 1
                self.b.append(self.cycles + self.rng.randint(2, 18))
        d.sys_clk.value = 1
        await Timer(4, unit="ns")
        return observed


@cocotb.test()
async def randomized_memory_roundtrips(dut):
    bus = MemoryBus(dut)
    await bus.reset()
    # Include zero length, one beat, odd sizes and sizes larger than max_reads.
    for operation, size in enumerate((0, 1, 7, 33, 64)):
        start = 0x100 + operation * 0x1000
        values = [bus.rng.getrandbits(len(dut.s_data)) for _ in range(size)]
        dut.we.value = 1
        dut.adr.value = start
        dut.beats.value = size
        dut.start.value = 1
        await bus.tick()
        dut.start.value = 0
        pending = None
        index = 0
        for _ in range(10000):
            if pending is None and index < size and bus.rng.random() > 0.3:
                pending = values[index]
            dut.s_valid.value = int(pending is not None)
            dut.s_data.value = pending or 0
            observed = await bus.tick()
            if pending is not None and observed["s_ready"]:
                index += 1
                pending = None
            if not int(dut.busy.value):
                break
        else:
            assert False, "Fill timeout"
        dut.s_valid.value = 0
        assert index == size
        assert not bus.aw and not bus.w and not bus.b, "DMA retired before write responses"
        assert [bus.read(start + i * bus.bytes_per_beat) for i in range(size)] == values

        # Replace RAM independently so reads cannot pass just by echoing source data.
        expected = [bus.rng.getrandbits(len(dut.s_data)) for _ in range(size)]
        for i, value in enumerate(expected):
            for byte in range(bus.bytes_per_beat):
                bus.memory[start + i * bus.bytes_per_beat + byte] = value >> (8 * byte) & 255
        dut.we.value = 0
        dut.last_en.value = 1
        dut.start.value = 1
        await bus.tick()
        dut.start.value = 0
        captured, last_at = [], []
        held = None
        for _ in range(10000):
            ready = bus.rng.random() > 0.5
            dut.m_ready.value = int(ready)
            observed = await bus.tick()
            if held is not None:
                assert observed["m_valid"] and (observed["m_data"], observed["m_last"]) == held
            held = (observed["m_data"], observed["m_last"]) if observed["m_valid"] and not ready else None
            if observed["m_valid"] and ready:
                captured.append(observed["m_data"])
                if observed["m_last"]:
                    last_at.append(len(captured) - 1)
            if not int(dut.busy.value):
                break
        else:
            assert False, "Drain timeout"
        assert captured == expected
        assert last_at == ([size - 1] if size else [])
        assert not bus.ar and bus.held_r is None, "DMA retired with reads pending"
    assert bus.write_count == bus.responses == bus.read_count == 105
