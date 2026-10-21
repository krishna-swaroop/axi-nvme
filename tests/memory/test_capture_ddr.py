"""Independent W08-A tests for the writer and real host-memory responder.

Every byte comparison is against test-generated source bytes or explicit guard
fixtures. The tests do not inspect writer state or use the RAM dictionary as a
readback oracle; payload readback traverses the exported PCIe request and
completion interface of the generated responder.
"""

from __future__ import annotations

import os
import random

import cocotb
from cocotb.triggers import Timer

from axi_ram import AxiRam


def _env_int(name: str, default: int) -> int:
    return int(os.environ.get(name, str(default)), 0)


class DDRBench:
    """Bounded manual-clock driver for the public ``ddr_tb`` pins."""

    INPUTS = (
        "rst", "job_valid", "job_slot", "job_lease", "job_epoch", "job_sequence",
        "s_valid", "s_data", "s_last", "s_sequence", "commit_ready",
        "visibility_ready", "abort_req", "recover_valid", "fence_done",
        "req_valid", "req_first", "req_last", "req_req_id", "req_tc", "req_attr",
        "req_we", "req_adr", "req_len", "req_first_be", "req_last_be", "req_tag",
        "req_dat", "req_channel", "req_user_id", "cpl_ready",
    )
    OUTPUTS = (
        "job_ready", "s_ready", "commit_valid", "commit_slot", "commit_lease",
        "commit_epoch", "commit_sequence", "faulted", "error_code", "axi_quiescent",
        "stream_drained", "recover_ready", "req_ready", "cpl_valid", "cpl_first",
        "cpl_last", "cpl_req_id", "cpl_cmp_id", "cpl_tc", "cpl_attr", "cpl_adr",
        "cpl_len", "cpl_byte_count", "cpl_end", "cpl_err", "cpl_tag", "cpl_dat",
        "cpl_channel", "cpl_user_id",
    )
    AXI_PINS = tuple(
        f"mem_{channel}_{field}"
        for channel, fields in {
            "aw": ("valid", "ready", "addr", "len", "size", "burst", "id"),
            "w": ("valid", "ready", "data", "strb", "last"),
            "b": ("valid", "ready", "resp", "id"),
            "ar": ("valid", "ready", "addr", "len", "size", "burst", "id"),
            "r": ("valid", "ready", "data", "resp", "last", "id"),
        }.items()
        for field in fields
    )

    def __init__(
        self,
        dut,
        *,
        b_delay: tuple[int, int] = (2, 5),
        r_delay: tuple[int, int] = (2, 5),
        write_resp_errors_at: set[int] | None = None,
        write_id_errors_at: set[int] | None = None,
        read_resp_errors_at: set[int] | None = None,
    ):
        self.dut = dut
        self.data_width = len(dut.s_data)
        self.beat_bytes = self.data_width // 8
        self.chunk_beats = _env_int("DDR_CHUNK_BEATS", 19)
        self.slots = _env_int("DDR_SLOTS", 3)
        self.slot_stride = _env_int("DDR_SLOT_STRIDE_BYTES", 4096)
        self.region_base = _env_int("DDR_REGION_BASE", 4096 - self.beat_bytes)
        self.aperture = _env_int("DDR_APERTURE_BYTES", 65536)
        self.max_burst = _env_int("DDR_MAX_BURST_BEATS", 16)
        self.pcie_base = _env_int("DDR_PCIE_BASE", 0x10000000)
        self.seed = _env_int("DDR_SEED", 20261021 + self.data_width)
        self.random = random.Random(self.seed)
        self.inputs = {name: 0 for name in self.INPUTS}
        self.ram = AxiRam(
            dut,
            data_width=self.data_width,
            aperture_bytes=self.aperture,
            seed=self.seed ^ 0xA51C_4A7,
            b_delay=b_delay,
            r_delay=r_delay,
            write_resp_errors_at=write_resp_errors_at,
            write_id_errors_at=write_id_errors_at,
            read_resp_errors_at=read_resp_errors_at,
        )

    def _drive_inputs(self) -> None:
        for name, value in self.inputs.items():
            getattr(self.dut, name).value = value

    def _sample(self) -> dict[str, int]:
        names = (*self.OUTPUTS, *self.AXI_PINS,
                 "wr_axi_aw_valid", "wr_axi_aw_ready", "wr_axi_aw_addr",
                 "wr_axi_aw_len", "wr_axi_w_valid", "wr_axi_w_ready",
                 "wr_axi_b_valid", "wr_axi_b_ready")
        result = {}
        for name in names:
            gate = None
            if name.startswith("cpl_") and name not in ("cpl_valid", "cpl_ready"):
                gate = "cpl_valid"
            elif name.startswith("mem_"):
                _, channel, field = name.split("_", 2)
                if field not in ("valid", "ready"):
                    gate = f"mem_{channel}_valid"
            elif name.startswith("wr_axi_") and name.split("_")[-1] not in ("valid", "ready"):
                gate = "_".join(name.split("_")[:3]) + "_valid"
            # Uninitialized FIFO payload is legal while VALID=0. Never coerce an
            # unknown on a live transfer, or on a handshake/control signal.
            if gate and not int(getattr(self.dut, gate).value):
                result[name] = 0
            else:
                try:
                    result[name] = int(getattr(self.dut, name).value)
                except ValueError as exc:
                    raise AssertionError(f"unknown active/control signal {name}") from exc
        return result

    async def reset(self) -> None:
        """Apply reset without sampling unresolved pre-reset DUT outputs."""
        self.dut.clk.value = 0
        self.inputs = {name: 0 for name in self.INPUTS}
        self.inputs["rst"] = 1
        self.ram.enabled = False
        self._drive_inputs()
        for _ in range(3):
            self.dut.clk.value = 0
            self.ram.drive()
            await Timer(5, unit="ns")
            self.dut.clk.value = 1
            await Timer(5, unit="ns")
        self.inputs["rst"] = 0
        self._drive_inputs()
        self.ram.enabled = True
        # One settled post-reset edge initializes every sampled output.
        await self.tick()

    async def tick(self, **changes) -> dict[str, int]:
        self.inputs.update(changes)
        self.dut.clk.value = 0
        self._drive_inputs()
        self.ram.drive()
        await Timer(5, unit="ns")
        values = self._sample()
        self.ram.observe(values)
        self.dut.clk.value = 1
        await Timer(5, unit="ns")
        self.ram.accept(values)
        return values

    async def until(self, predicate, *, message: str, cycles: int = 5000, **changes):
        for _ in range(cycles):
            values = await self.tick(**changes)
            if predicate(values):
                return values
        raise AssertionError(f"timeout waiting for {message} after {cycles} cycles")

    @property
    def chunk_bytes(self) -> int:
        return self.chunk_beats * self.beat_bytes

    def first_burst_beats(self, slot: int = 0) -> int:
        address = self.region_base + slot * self.slot_stride
        to_boundary = (4096 - (address & 0xFFF)) // self.beat_bytes
        return min(self.max_burst, self.chunk_beats, to_boundary)

    def make_payload(self, seed: int) -> tuple[bytes, list[int]]:
        rng = random.Random(seed)
        payload = bytes(rng.randrange(256) for _ in range(self.chunk_bytes))
        words = [
            int.from_bytes(payload[offset:offset + self.beat_bytes], "little")
            for offset in range(0, len(payload), self.beat_bytes)
        ]
        return payload, words

    async def start_job(self, *, slot: int = 0, lease: int = 0x1234,
                        epoch: int = 7, sequence: int = 11) -> None:
        self.inputs.update({
            "job_valid": 1,
            "job_slot": slot,
            "job_lease": lease,
            "job_epoch": epoch,
            "job_sequence": sequence,
        })
        values = await self.until(lambda sample: sample["job_ready"], message="job ready")
        assert values["job_ready"] == 1
        self.inputs["job_valid"] = 0
        await self.tick()

    async def send_beats(self, words: list[int], *, sequence: int,
                         start_index: int = 0, last_index: int | None = None,
                         sequence_override: int | None = None,
                         final_last_override: int | None = None) -> int:
        """Send until every requested beat handshakes; return the next index."""
        if last_index is None:
            last_index = len(words) - 1
        index = start_index
        while index <= last_index:
            seq = sequence if sequence_override is None else sequence_override
            values = await self.tick(
                s_valid=1,
                s_data=words[index],
                s_last=(final_last_override if index == len(words) - 1
                        and final_last_override is not None
                        else int(index == len(words) - 1)),
                s_sequence=seq,
            )
            if values["s_ready"]:
                index += 1
        self.inputs["s_valid"] = 0
        self.inputs["s_last"] = 0
        return index

    async def send_full_chunk(self, words: list[int], *, sequence: int) -> None:
        await self.send_beats(words, sequence=sequence)

    async def drain_to_tlast(self, words: list[int], *, sequence: int,
                             start_index: int) -> None:
        await self.send_beats(words, sequence=sequence, start_index=start_index)

    async def wait_commit(self, *, slot: int, lease: int, epoch: int, sequence: int,
                          visibility: int = 1, accept: bool = True) -> dict[str, int]:
        self.inputs["visibility_ready"] = visibility
        self.inputs["commit_ready"] = 0
        values = await self.until(
            lambda sample: sample["commit_valid"],
            message="commit valid",
            cycles=max(5000, self.chunk_beats * 40),
        )
        expected = (slot, lease, epoch, sequence)
        got = (values["commit_slot"], values["commit_lease"],
               values["commit_epoch"], values["commit_sequence"])
        assert got == expected, f"commit handle {got} != {expected}"
        for _ in range(3):
            held = await self.tick()
            assert held["commit_valid"] == 1
            assert (held["commit_slot"], held["commit_lease"], held["commit_epoch"],
                    held["commit_sequence"]) == expected
        if accept:
            values = await self.tick(commit_ready=1)
            assert values["commit_valid"] == 1
            self.inputs["commit_ready"] = 0
            await self.tick()
        return values

    async def recover(self, *, try_job_on_edge: bool = False) -> None:
        values = await self.until(
            lambda sample: sample["axi_quiescent"] and sample["stream_drained"],
            message="AXI quiescence and source drain",
            cycles=max(5000, self.chunk_beats * 40),
        )
        assert values["faulted"] == 1
        self.inputs.update({"recover_valid": 1, "fence_done": 1})
        if try_job_on_edge:
            self.inputs.update({"job_valid": 1, "job_slot": 0, "job_lease": 0x999,
                                "job_epoch": 9, "job_sequence": 99})
        values = await self.tick()
        assert values["recover_ready"] == 1
        assert values["job_ready"] == 0, "job was accepted on recovery edge"
        self.inputs.update({"recover_valid": 0, "fence_done": 0, "job_valid": 0})
        values = await self.tick()
        assert values["faulted"] == 0
        assert values["error_code"] == 0

    async def read_dwords(self, address: int, count: int, *, expect_error: bool = False,
                          tag: int = 0x5A,
                          expected_axi_r_beats: int | None = None) -> bytes:
        assert 1 <= count <= 64, "the first gate uses only 1..64-dword MemRd requests"
        if expected_axi_r_beats is not None:
            assert expected_axi_r_beats > 0
        assert address % 4 == 0
        r_start = len(self.ram.r_cycles)
        self.inputs.update({
            "req_valid": 1,
            "req_first": 1,
            "req_last": 1,
            "req_req_id": 0x321,
            "req_tc": 0,
            "req_attr": 0,
            "req_we": 0,
            "req_adr": self.pcie_base + address,
            "req_len": count,
            "req_first_be": 0xF,
            "req_last_be": 0xF,
            "req_tag": tag,
            "req_dat": 0,
            "req_channel": 0,
            "req_user_id": 0,
            "cpl_ready": 0,
        })
        values = await self.until(lambda sample: sample["req_ready"],
                                  message="host-memory MemRd acceptance")
        assert values["req_ready"] == 1
        self.inputs["req_valid"] = 0
        self.ram.force_ready["ar"] = False
        ar_values = await self.until(lambda sample: sample["mem_ar_valid"],
                                     message="responder AXI AR")
        if expected_axi_r_beats is not None:
            assert ar_values["cpl_valid"] == 0, "completion appeared before any AXI R beat"
        for _ in range(2):
            stalled = await self.tick()
            assert stalled["mem_ar_valid"] == 1
            assert stalled["mem_ar_ready"] == 0
            if expected_axi_r_beats is not None:
                assert stalled["cpl_valid"] == 0, "completion appeared before any AXI R beat"
        self.ram.force_ready["ar"] = True

        dwords: list[int] = []
        saw_first = False
        final = None
        held_completion = None
        completion_fields = (
            "cpl_first", "cpl_last", "cpl_req_id", "cpl_cmp_id", "cpl_tc", "cpl_attr",
            "cpl_adr", "cpl_len", "cpl_byte_count", "cpl_end", "cpl_err", "cpl_tag",
            "cpl_dat", "cpl_channel", "cpl_user_id",
        )
        for cycle in range(1000):
            # Deterministic backpressure makes completion payload stability observable.
            ready = int((cycle % 5) not in (1, 2))
            accepted_r_before = len(self.ram.r_cycles) - r_start
            values = await self.tick(cpl_ready=ready)
            if values["cpl_valid"] and expected_axi_r_beats is not None:
                assert accepted_r_before == expected_axi_r_beats, (
                    "completion became valid before every expected AXI R beat retired: "
                    f"{accepted_r_before} != {expected_axi_r_beats}"
                )
            completion = tuple(values[name] for name in completion_fields)
            if held_completion is not None:
                assert values["cpl_valid"] == 1 and completion == held_completion, (
                    "host-memory completion changed while stalled"
                )
            if values["cpl_valid"] and not ready:
                held_completion = completion
            else:
                held_completion = None
            if values["cpl_valid"] and ready:
                assert values["cpl_req_id"] == 0x321
                assert values["cpl_tag"] == tag
                assert values["cpl_len"] == (0 if expect_error else count)
                assert values["cpl_byte_count"] == (0 if expect_error else count * 4)
                assert values["cpl_cmp_id"] == 0
                assert values["cpl_adr"] == (0 if expect_error else ((self.pcie_base + address) & 0x7F))
                assert values["cpl_tc"] == 0 and values["cpl_attr"] == 0
                assert values["cpl_channel"] == 0 and values["cpl_user_id"] == 0
                assert bool(values["cpl_err"]) is expect_error
                assert bool(values["cpl_first"]) is (not saw_first)
                saw_first = True
                data = values["cpl_dat"]
                for lane in range(self.beat_bytes // 4):
                    dwords.append((data >> (lane * 32)) & 0xFFFF_FFFF)
                if values["cpl_last"]:
                    assert values["cpl_end"] == 1
                    final = values
                    break
        else:
            raise AssertionError("timeout waiting for host-memory completion")
        assert final is not None and saw_first
        if expect_error:
            assert len(dwords) == self.beat_bytes // 4
            assert all(word == 0 for word in dwords)
            assert final["cpl_first"] == final["cpl_last"] == final["cpl_end"] == 1
            return b""
        assert len(dwords) >= count
        return b"".join(word.to_bytes(4, "little") for word in dwords[:count])

    async def wait_fault(self, expected_code: int) -> dict[str, int]:
        values = await self.until(lambda sample: sample["faulted"],
                                  message=f"fault code {expected_code}",
                                  cycles=max(5000, self.chunk_beats * 40))
        assert values["error_code"] == expected_code, (
            f"error code {values['error_code']} != {expected_code}"
        )
        assert values["commit_valid"] == 0
        return values


def _guard_payload(seed: int, length: int) -> bytes:
    rng = random.Random(seed)
    return bytes(rng.randrange(256) for _ in range(length))


@cocotb.test(timeout_time=10, timeout_unit="ms")
async def test_shared_ram_byte_exact_readback_visibility_and_4k(dut):
    """Arbitrary writer bytes round-trip via the actual responder and shared RAM."""
    tb = DDRBench(dut, b_delay=(100, 100), r_delay=(3, 7))
    await tb.reset()
    assert tb.chunk_beats > 0 and tb.chunk_beats <= 128
    payload, words = tb.make_payload(0xDDF0_0000 + tb.data_width)
    address = tb.region_base
    before_addr = address - 16 if address >= 16 else None
    after_addr = address + len(payload)
    before = _guard_payload(0xBEEF + tb.data_width, 16) if before_addr is not None else b""
    after = _guard_payload(0xCAFE + tb.data_width, 16)
    if before_addr is not None:
        tb.ram.seed_bytes(before_addr, before)
    tb.ram.seed_bytes(after_addr, after)
    sentinel_addr = 0x2000
    sentinel = _guard_payload(0x51A7 + tb.data_width, 32)
    tb.ram.seed_bytes(sentinel_addr, sentinel)

    tb.ram.force_ready.update({"aw": False, "w": False})
    await tb.start_job(sequence=0xA17)
    first = tb.first_burst_beats()
    assert first > 0
    await tb.send_beats(words, sequence=0xA17, last_index=first - 1)
    values = await tb.until(lambda sample: sample["mem_aw_valid"] and sample["mem_w_valid"],
                            message="first advertised AXI burst")
    held_aw = tuple(values[f"mem_aw_{field}"] for field in ("addr", "len", "size", "burst", "id"))
    held_w = tuple(values[f"mem_w_{field}"] for field in ("data", "strb", "last"))
    for _ in range(3):
        held = await tb.tick()
        assert held["mem_aw_valid"] and held["mem_w_valid"]
        assert tuple(held[f"mem_aw_{field}"] for field in ("addr", "len", "size", "burst", "id")) == held_aw
        assert tuple(held[f"mem_w_{field}"] for field in ("data", "strb", "last")) == held_w

    # Let every W beat overtake its stalled AW. The model must retain independent
    # channel queues and pair the beats only after the address arrives.
    tb.ram.force_ready.update({"aw": False, "w": True})
    await tb.until(lambda _sample: tb.ram.w_before_aw_beats >= first,
                   message="first burst W beats before AW")
    assert not tb.ram.write_transactions
    tb.ram.force_ready["aw"] = True
    # The first read request is accepted while the writer's first B response is
    # deliberately delayed, exercising the responder and writer through the
    # shared LiteX arbiter/RAM rather than serializing their interfaces.
    await tb.until(lambda _sample: len(tb.ram.write_transactions) >= 1,
                   message="first shared-RAM write transaction")
    assert len(tb.ram.b_cycles) == 0, "test did not hold B long enough for overlap"
    overlap = await tb.read_dwords(sentinel_addr, 8, tag=0x31)
    assert overlap == sentinel[:32]
    assert tb.ram.ar_cycles and not tb.ram.b_cycles, (
        "the responder read did not complete before the writer's delayed B response"
    )

    await tb.send_beats(words, sequence=0xA17, start_index=first)
    tb.inputs["visibility_ready"] = 0
    values = await tb.until(lambda sample: sample["axi_quiescent"],
                            message="all writer AXI bursts retire",
                            cycles=max(5000, tb.chunk_beats * 40))
    assert values["faulted"] == 0
    assert values["commit_valid"] == 0, "publication passed a withheld visibility guard"
    assert len(tb.ram.write_transactions) == len(tb.ram.b_cycles)
    txns = tb.ram.write_transactions
    assert sum(item["beats"] for item in txns) == tb.chunk_beats
    assert txns[0]["addr"] == address
    for txn in txns:
        assert txn["id"] == 0
        assert txn["size"] == (tb.beat_bytes.bit_length() - 1)
        assert txn["burst"] == 1
        assert 1 <= txn["beats"] <= tb.max_burst
        assert all(mask == (1 << tb.beat_bytes) - 1 for mask in txn["strobes"])
        assert (txn["addr"] // 4096) == ((txn["addr"] + txn["beats"] * tb.beat_bytes - 1) // 4096)
    if (address & 0xFFF) + tb.chunk_bytes > 4096:
        assert txns[0]["beats"] == tb.first_burst_beats()
        assert txns[0]["addr"] + txns[0]["beats"] * tb.beat_bytes == ((address // 4096) + 1) * 4096

    await tb.wait_commit(slot=0, lease=0x1234, epoch=7, sequence=0xA17,
                         visibility=1, accept=True)
    expected_sentinel_reads = ((sentinel_addr % tb.beat_bytes) + len(sentinel)
                               + tb.beat_bytes - 1) // tb.beat_bytes
    assert len(tb.ram.read_transactions) == expected_sentinel_reads
    assert tb.ram.ar_cycles[0] < tb.ram.b_cycles[0], (
        "the sentinel responder read did not overlap the writer's first outstanding B"
    )
    assert tb.ram.aw_stall_cycles >= 3 and tb.ram.w_stall_cycles >= 3
    assert tb.ram.ar_stall_cycles >= 2
    assert tb.ram.b_wait_cycles > 0

    dword_count = len(payload) // 4
    # Read every payload dword in legal <=64-dword requests, starting first at
    # a nonzero dword offset so the real responder's realignment path is covered.
    collected = bytearray()
    offset = 1
    remaining = dword_count - offset
    while remaining:
        count = min(64, remaining)
        part = await tb.read_dwords(address + offset * 4, count, tag=(0x40 + offset) & 0xFF)
        collected.extend(part)
        offset += count
        remaining -= count
    assert bytes(collected) == payload[4:], "responder readback differs from arbitrary source bytes"
    # A separate aligned read confirms byte zero and the whole first line.
    head_count = min(64, dword_count)
    assert await tb.read_dwords(address, head_count, tag=0x71) == payload[:head_count * 4]
    if before_addr is not None:
        assert await tb.read_dwords(before_addr, 4, tag=0x72) == before
    assert await tb.read_dwords(after_addr, 4, tag=0x73) == after


@cocotb.test(timeout_time=10, timeout_unit="ms")
async def test_bad_job_range_or_zero_lease_fails_before_axi(dut):
    """Reject an encodable invalid slot, or zero lease when slot width is exact."""
    tb = DDRBench(dut)
    await tb.reset()
    slot_width = len(dut.job_slot)
    representable_slots = 1 << slot_width
    bad_slot = tb.slots if tb.slots < representable_slots else None
    if bad_slot is not None:
        tb.inputs.update({"job_valid": 1, "job_slot": bad_slot, "job_lease": 1,
                          "job_epoch": 1, "job_sequence": 1})
    else:
        tb.inputs.update({"job_valid": 1, "job_slot": 0, "job_lease": 0,
                          "job_epoch": 1, "job_sequence": 1})
    values = await tb.tick()
    assert values["job_ready"] == 0
    values = await tb.wait_fault(1)
    assert values["commit_valid"] == 0
    assert not tb.ram.write_transactions and not tb.ram.aw_queue and not tb.ram.w_queue


@cocotb.test(timeout_time=10, timeout_unit="ms")
async def test_framing_faults_reject_early_last_and_sequence_mismatch(dut):
    """Bad framing and sequence never advertise a partial writer burst."""
    tb = DDRBench(dut)
    await tb.reset()
    _, words = tb.make_payload(0xF12A + tb.data_width)

    # Early TLAST is the faulting handshake; no complete buffered burst exists.
    await tb.start_job(slot=0, lease=0x111, epoch=1, sequence=22)
    values = await tb.tick(s_valid=1, s_data=words[0], s_last=1, s_sequence=22)
    tb.inputs.update({"s_valid": 0, "s_last": 0})
    assert values["s_ready"] == 1
    values = await tb.wait_fault(2)
    assert values["stream_drained"] == 1
    assert not tb.ram.write_transactions
    await tb.recover()

    # The second job has a correct boundary but a wrong sequence. Drain the rest
    # of that source chunk, then prove no stale payload became a later write.
    await tb.start_job(slot=0, lease=0x112, epoch=1, sequence=23)
    values = await tb.tick(s_valid=1, s_data=words[0], s_last=0, s_sequence=24)
    tb.inputs["s_valid"] = 0
    assert values["s_ready"] == 1
    await tb.wait_fault(2)
    await tb.drain_to_tlast(words, sequence=23, start_index=1)
    values = await tb.until(lambda sample: sample["stream_drained"], message="faulted chunk drain")
    assert values["commit_valid"] == 0
    assert not tb.ram.write_transactions
    await tb.recover(try_job_on_edge=True)
    assert not tb.ram.write_transactions


@cocotb.test(timeout_time=10, timeout_unit="ms")
async def test_missing_tlast_on_final_beat_faults_then_drains_residue(dut):
    """A missing final TLAST rejects the handle and drains one trailing beat."""
    tb = DDRBench(dut, b_delay=(2, 4))
    await tb.reset()
    _, words = tb.make_payload(0x1A57 + tb.data_width)
    await tb.start_job(slot=0, lease=0x118, epoch=2, sequence=25)
    await tb.send_beats(words, sequence=25, final_last_override=0)
    values = await tb.wait_fault(2)
    assert values["stream_drained"] == 0, "late/missing TLAST did not keep source ownership active"
    assert values["commit_valid"] == 0

    residue = tb.random.getrandbits(tb.data_width)
    values = await tb.tick(s_valid=1, s_data=residue, s_last=1, s_sequence=25)
    assert values["s_ready"] == 1
    values = await tb.until(lambda sample: sample["stream_drained"] and sample["axi_quiescent"],
                            message="residue drain and prior B retirement")
    assert values["commit_valid"] == 0
    assert len(tb.ram.write_transactions) == len(tb.ram.b_cycles)

    # Complete bursts before the bad final beat may have reached memory, but the
    # incomplete final burst must never be advertised and no handle is published.
    expected_completed = 0
    next_addr = tb.region_base
    remaining = tb.chunk_beats - 1
    while remaining:
        to_boundary = (4096 - (next_addr & 0xFFF)) // tb.beat_bytes
        burst = min(tb.max_burst, tb.chunk_beats - expected_completed, to_boundary)
        if burst <= 0:
            break
        # The bad final beat itself is not buffered, so a short last burst stays local.
        if burst > remaining:
            break
        expected_completed += burst
        remaining -= burst
        next_addr += burst * tb.beat_bytes
    assert sum(txn["beats"] for txn in tb.ram.write_transactions) == expected_completed
    assert expected_completed < tb.chunk_beats


@cocotb.test(timeout_time=10, timeout_unit="ms")
async def test_axi_bresp_error_faults_before_commit_and_later_bursts(dut):
    """An AXI SLVERR prevents commit and stops later chunk bursts."""
    tb = DDRBench(dut, write_resp_errors_at={0}, b_delay=(2, 2))
    await tb.reset()
    _, words = tb.make_payload(0xBADC0DE + tb.data_width)
    await tb.start_job(sequence=31)
    await tb.send_full_chunk(words, sequence=31)
    values = await tb.wait_fault(3)
    assert values["stream_drained"] == 1
    assert len(tb.ram.write_transactions) == 1
    assert tb.ram.write_transactions[0]["bresp"] == 2
    assert not values["commit_valid"]
    await tb.recover()
    await tb.tick()
    assert len(tb.ram.write_transactions) == 1, "writer issued a later burst after B error"


@cocotb.test(timeout_time=10, timeout_unit="ms")
async def test_axi_bid_error_faults_before_commit_and_later_bursts(dut):
    """A mismatched B ID faults the job even when the response is otherwise OK."""
    tb = DDRBench(dut, write_id_errors_at={0}, b_delay=(2, 2))
    await tb.reset()
    _, words = tb.make_payload(0x1D0B + tb.data_width)
    await tb.start_job(sequence=32)
    await tb.send_full_chunk(words, sequence=32)
    values = await tb.wait_fault(3)
    assert values["stream_drained"] == 1
    assert len(tb.ram.write_transactions) == 1
    assert tb.ram.write_transactions[0]["bresp"] == 0
    assert tb.ram.write_transactions[0]["b_id"] != 0
    assert not values["commit_valid"]
    await tb.recover()
    await tb.tick()
    assert len(tb.ram.write_transactions) == 1


@cocotb.test(timeout_time=10, timeout_unit="ms")
async def test_abort_during_collect_discards_prefix_and_recovers_clean_handle(dut):
    """Aborted source residue is drained; the next handle stores only new bytes."""
    tb = DDRBench(dut)
    await tb.reset()
    old_payload, old_words = tb.make_payload(0xAB07 + tb.data_width)
    new_payload, new_words = tb.make_payload(0xC1EA + tb.data_width)
    await tb.start_job(slot=0, lease=0x201, epoch=3, sequence=41)
    prefix = max(0, tb.first_burst_beats() - 1)
    if prefix:
        await tb.send_beats(old_words, sequence=41, last_index=prefix - 1)
    values = await tb.tick(abort_req=1)
    assert values["faulted"] == 0
    tb.inputs["abort_req"] = 0
    await tb.wait_fault(4)
    await tb.drain_to_tlast(old_words, sequence=41, start_index=prefix)
    values = await tb.until(lambda sample: sample["stream_drained"], message="aborted source drain")
    assert values["axi_quiescent"] == 1
    assert not tb.ram.write_transactions, "collect-stage abort issued an AXI burst"
    await tb.recover(try_job_on_edge=True)

    await tb.start_job(slot=1, lease=0x202, epoch=4, sequence=42)
    await tb.send_full_chunk(new_words, sequence=42)
    await tb.wait_commit(slot=1, lease=0x202, epoch=4, sequence=42)
    address = tb.region_base + tb.slot_stride
    expected = new_payload
    for offset in range(0, len(expected) // 4, 64):
        count = min(64, len(expected) // 4 - offset)
        got = await tb.read_dwords(address + 4 * offset, count, tag=(0x80 + offset // 64))
        assert got == expected[4 * offset:4 * (offset + count)]
    assert len(old_payload) == len(new_payload)


@cocotb.test(timeout_time=10, timeout_unit="ms")
async def test_abort_finishes_stalled_advertised_burst_then_drains(dut):
    """Abort does not withdraw a stalled AW/W and issues no later burst."""
    tb = DDRBench(dut, b_delay=(12, 12))
    await tb.reset()
    _, words = tb.make_payload(0xA80B7 + tb.data_width)
    await tb.start_job(sequence=51)
    first = tb.first_burst_beats()
    await tb.send_beats(words, sequence=51, last_index=first - 1)
    tb.ram.force_ready.update({"aw": False, "w": False})
    values = await tb.until(lambda sample: sample["mem_aw_valid"] and sample["mem_w_valid"],
                            message="advertised AW/W before abort")
    held_aw = (values["mem_aw_addr"], values["mem_aw_len"], values["mem_aw_size"],
               values["mem_aw_burst"], values["mem_aw_id"])
    held_w = (values["mem_w_data"], values["mem_w_strb"], values["mem_w_last"])
    values = await tb.tick(abort_req=1)
    assert values["mem_aw_valid"] and values["mem_w_valid"]
    assert (values["mem_aw_addr"], values["mem_aw_len"], values["mem_aw_size"],
            values["mem_aw_burst"], values["mem_aw_id"]) == held_aw
    assert (values["mem_w_data"], values["mem_w_strb"], values["mem_w_last"]) == held_w
    tb.inputs["abort_req"] = 0
    await tb.wait_fault(4)
    await tb.drain_to_tlast(words, sequence=51, start_index=first)
    tb.ram.force_ready.update({"aw": True, "w": True})
    values = await tb.until(lambda sample: sample["axi_quiescent"] and sample["stream_drained"],
                            message="aborted burst B and source drain")
    assert values["faulted"] == 1
    assert len(tb.ram.write_transactions) == 1
    assert len(tb.ram.b_cycles) == 1
    assert values["commit_valid"] == 0
    await tb.recover()
    assert len(tb.ram.write_transactions) == 1


@cocotb.test(timeout_time=10, timeout_unit="ms")
async def test_abort_while_waiting_delayed_b_drains_source(dut):
    """Abort during a pending B response retains its obligation and drains input."""
    tb = DDRBench(dut, b_delay=(50, 50))
    await tb.reset()
    _, words = tb.make_payload(0xB00B + tb.data_width)
    await tb.start_job(sequence=56)
    first = tb.first_burst_beats()
    await tb.send_beats(words, sequence=56, last_index=first - 1)
    tb.ram.force_ready.update({"aw": True, "w": True})
    values = await tb.until(
        lambda sample: len(tb.ram.write_transactions) >= 1 and not sample["axi_quiescent"],
        message="writer waiting for delayed B response",
    )
    assert not tb.ram.b_cycles, "B response retired before the abort was applied"
    assert tb.ram.b_queue, "no B response obligation was pending"
    values = await tb.tick(abort_req=1)
    assert values["faulted"] == 0
    tb.inputs["abort_req"] = 0
    await tb.wait_fault(4)
    await tb.drain_to_tlast(words, sequence=56, start_index=first)
    values = await tb.until(lambda sample: sample["axi_quiescent"] and sample["stream_drained"],
                            message="aborted delayed B and source drain",
                            cycles=max(5000, tb.chunk_beats * 40))
    assert values["faulted"] == 1
    assert len(tb.ram.write_transactions) == 1
    assert len(tb.ram.b_cycles) == 1
    assert values["commit_valid"] == 0
    await tb.recover()


@cocotb.test(timeout_time=10, timeout_unit="ms")
async def test_abort_cancels_unpublished_stalled_commit(dut):
    """An explicit abort may cancel a visible-but-unaccepted commit handle."""
    tb = DDRBench(dut)
    await tb.reset()
    _, words = tb.make_payload(0xC01117 + tb.data_width)
    await tb.start_job(sequence=61)
    await tb.send_full_chunk(words, sequence=61)
    values = await tb.until(lambda sample: sample["axi_quiescent"],
                            message="all write responses before commit", cycles=10000)
    assert values["faulted"] == 0
    tb.inputs.update({"visibility_ready": 1, "commit_ready": 0})
    values = await tb.until(lambda sample: sample["commit_valid"], message="stalled commit")
    assert values["commit_lease"] == 0x1234
    for _ in range(3):
        values = await tb.tick()
        assert values["commit_valid"] == 1
    values = await tb.tick(abort_req=1, commit_ready=1)
    assert values["faulted"] == 0
    assert values["commit_valid"] == 0, "abort and commit_ready must not handshake a commit"
    tb.inputs.update({"abort_req": 0, "commit_ready": 0})
    values = await tb.wait_fault(4)
    assert values["stream_drained"] == 1 and values["axi_quiescent"] == 1
    assert values["commit_valid"] == 0
    await tb.recover(try_job_on_edge=True)


@cocotb.test(timeout_time=10, timeout_unit="ms")
async def test_responder_propagates_axi_read_error_to_completion(dut):
    """A late backend RRESP error poisons the whole read and leaves no stale state."""
    tb = DDRBench(dut, r_delay=(2, 2))
    await tb.reset()
    address = tb.region_base + 0x104
    count = 16
    beat_dwords = tb.beat_bytes // 4
    offset_dwords = (address % tb.beat_bytes) // 4
    expected_r_beats = (offset_dwords + count + beat_dwords - 1) // beat_dwords
    tb.ram.read_resp_errors_at = {expected_r_beats - 1}
    data = _guard_payload(0xE220 + tb.data_width, count * 4)
    tb.ram.seed_bytes(address, data)
    result = await tb.read_dwords(address, count, expect_error=True, tag=0xE2,
                                  expected_axi_r_beats=expected_r_beats)
    assert result == b"", "failed responder read exposed a data payload"
    assert len(tb.ram.read_transactions) == expected_r_beats
    assert [txn["resp"] for txn in tb.ram.read_transactions] == (
        [0] * (expected_r_beats - 1) + [2]
    )

    # The next healthy read uses the same range and the next transaction ordinal.
    # It must return the original bytes after the error CQE and all AXI traffic retire.
    recovered = await tb.read_dwords(address, count, tag=0xE3,
                                     expected_axi_r_beats=expected_r_beats)
    assert recovered == data, "healthy read after a failed request retained stale poison/data"
    assert len(tb.ram.read_transactions) == 2 * expected_r_beats
    assert [txn["resp"] for txn in tb.ram.read_transactions[expected_r_beats:]] == [
        0
    ] * expected_r_beats
