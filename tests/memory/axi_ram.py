"""Independent, byte-addressed AXI RAM model for the W08-A simulation.

The model observes only the public AXI slave pins. AW and W are accepted into
separate queues and paired in order, so W may arrive before AW. The byte
dictionary below is the one shared memory used by both the capture writer and
the real host-memory responder; readback tests consume data returned through
that responder rather than consulting a writer-side copy.
"""

from __future__ import annotations

from collections import deque
import random
from typing import Any


class AxiProtocolError(AssertionError):
    """Raised when a public AXI transfer violates the W08-A RAM contract."""


class AxiRam:
    """A deterministic, finite AXI4 slave with independent AW/W channels.

    Parameters are intentionally configurable so the testbench can choose a
    seed, bounded response latency, ready probabilities, and response faults.
    Error indices are zero-based transaction ordinals. An injected failed B
    response still applies the accepted W data to this synthetic RAM: an AXI
    error does not imply that a real memory made no partial changes.
    """

    def __init__(
        self,
        dut: Any,
        *,
        data_width: int,
        aperture_bytes: int,
        seed: int,
        ready_probability: dict[str, float] | None = None,
        b_delay: tuple[int, int] = (1, 4),
        r_delay: tuple[int, int] = (1, 4),
        write_resp_errors_at: set[int] | None = None,
        write_id_errors_at: set[int] | None = None,
        read_resp_errors_at: set[int] | None = None,
    ) -> None:
        if data_width not in (64, 128, 256):
            raise ValueError(f"unsupported test AXI data width {data_width}")
        if aperture_bytes <= 0:
            raise ValueError("aperture_bytes must be positive")
        self.dut = dut
        self.data_width = data_width
        self.data_bytes = data_width // 8
        self.full_strobe = (1 << self.data_bytes) - 1
        self.aperture_bytes = aperture_bytes
        self.random = random.Random(seed)
        self.ready_probability = ready_probability or {"aw": 0.65, "w": 0.65, "ar": 0.65}
        for channel in ("aw", "w", "ar"):
            probability = self.ready_probability[channel]
            if not 0.0 <= probability <= 1.0:
                raise ValueError(f"invalid {channel} ready probability {probability}")
        self.b_delay = b_delay
        self.r_delay = r_delay
        self.write_resp_errors_at = set(write_resp_errors_at or ())
        self.write_id_errors_at = set(write_id_errors_at or ())
        self.read_resp_errors_at = set(read_resp_errors_at or ())

        # This is the only memory store. Unwritten bytes are synthetic zeros.
        self.bytes: dict[int, int] = {}

        self.cycle = 0
        self.aw_queue: deque[dict[str, int]] = deque()
        self.w_queue: deque[dict[str, int]] = deque()
        self.b_queue: deque[dict[str, int]] = deque()
        self.ar_queue: deque[dict[str, int]] = deque()
        self.r_active: dict[str, int] | None = None
        self.r_current: tuple[int, int, int, int] | None = None

        self.write_transactions: list[dict[str, Any]] = []
        self.read_transactions: list[dict[str, int]] = []
        self.aw_cycles: list[int] = []
        self.w_cycles: list[int] = []
        self.b_cycles: list[int] = []
        self.ar_cycles: list[int] = []
        self.r_cycles: list[int] = []
        self.aw_stall_cycles = 0
        self.w_stall_cycles = 0
        self.ar_stall_cycles = 0
        self.b_wait_cycles = 0
        self.b_stall_cycles = 0
        self.r_wait_cycles = 0
        self.r_stall_cycles = 0
        self.w_before_aw_beats = 0
        self._aw_id_width = len(dut.mem_aw_id)
        self._prev_stall: dict[str, tuple[int, ...] | None] = {"aw": None, "w": None, "ar": None}
        self._prev_response_stall: dict[str, tuple[int, ...] | None] = {"b": None, "r": None}
        self.force_ready: dict[str, bool | None] = {"aw": None, "w": None, "ar": None}
        self.enabled = False

    def _sig(self, channel: str, field: str):
        return getattr(self.dut, f"mem_{channel}_{field}")

    def _choose_ready(self, channel: str) -> int:
        forced = self.force_ready[channel]
        if forced is not None:
            return int(forced)
        return int(self.random.random() < self.ready_probability[channel])

    def seed_bytes(self, address: int, data: bytes) -> None:
        """Initialize synthetic RAM bytes for guard/read-request fixtures."""
        self._check_span(address, len(data), "seed")
        for index, value in enumerate(data):
            self.bytes[address + index] = value

    def read_bytes(self, address: int, length: int) -> bytes:
        """Return the model's byte contents for diagnostics only."""
        self._check_span(address, length, "read")
        return bytes(self.bytes.get(address + index, 0) for index in range(length))

    def drive(self) -> None:
        """Drive AXI slave outputs for the next rising edge."""
        if not self.enabled:
            for channel in ("aw", "w", "ar"):
                self._sig(channel, "ready").value = 0
            self._sig("b", "valid").value = 0
            self._sig("b", "resp").value = 0
            self._sig("b", "id").value = 0
            self._sig("r", "valid").value = 0
            self._sig("r", "data").value = 0
            self._sig("r", "resp").value = 0
            self._sig("r", "last").value = 0
            self._sig("r", "id").value = 0
            return

        for channel in ("aw", "w", "ar"):
            self._sig(channel, "ready").value = self._choose_ready(channel)

        b = self.b_queue[0] if self.b_queue and self.b_queue[0]["due"] <= self.cycle else None
        if b is None:
            self._sig("b", "valid").value = 0
            self._sig("b", "resp").value = 0
            self._sig("b", "id").value = 0
        else:
            self._sig("b", "valid").value = 1
            self._sig("b", "resp").value = b["resp"]
            self._sig("b", "id").value = b["id"]

        if self.r_active is None and self.ar_queue and self.ar_queue[0]["due"] <= self.cycle:
            self.r_active = self.ar_queue[0]
        if self.r_active is None:
            self._sig("r", "valid").value = 0
            self._sig("r", "data").value = 0
            self._sig("r", "resp").value = 0
            self._sig("r", "last").value = 0
            self._sig("r", "id").value = 0
        else:
            if self.r_current is None:
                index = self.r_active["index"]
                address = self.r_active["addr"] + index * self.data_bytes
                data = int.from_bytes(self.read_bytes(address, self.data_bytes), "little")
                self.r_current = (data, self.r_active["resp"],
                                  int(index == self.r_active["beats"] - 1),
                                  self.r_active["id"])
            data, resp, last, ident = self.r_current
            self._sig("r", "valid").value = 1
            self._sig("r", "data").value = data
            self._sig("r", "resp").value = resp
            self._sig("r", "last").value = last
            self._sig("r", "id").value = ident

    def observe(self, snapshot: dict[str, int]) -> None:
        """Check public AXI payload stability and accumulate stall evidence."""
        for channel, fields in {
            "aw": ("addr", "len", "size", "burst", "id"),
            "w": ("data", "strb", "last"),
            "ar": ("addr", "len", "size", "burst", "id"),
        }.items():
            valid = snapshot[f"mem_{channel}_valid"]
            ready = snapshot[f"mem_{channel}_ready"]
            payload = tuple(snapshot[f"mem_{channel}_{field}"] for field in fields)
            previous = self._prev_stall[channel]
            if previous is not None and (not valid or payload != previous):
                raise AxiProtocolError(f"mem {channel.upper()} changed while stalled: {previous} -> {payload}")
            self._prev_stall[channel] = payload if valid and not ready else None
            if valid and not ready:
                setattr(self, f"{channel}_stall_cycles", getattr(self, f"{channel}_stall_cycles") + 1)

        b_payload = (snapshot["mem_b_resp"], snapshot["mem_b_id"])
        previous_b = self._prev_response_stall["b"]
        if previous_b is not None and (not snapshot["mem_b_valid"] or b_payload != previous_b):
            raise AxiProtocolError("B response changed before its handshake")
        self._prev_response_stall["b"] = (
            b_payload if snapshot["mem_b_valid"] and not snapshot["mem_b_ready"] else None
        )
        if snapshot["mem_b_valid"] and not snapshot["mem_b_ready"]:
            self.b_stall_cycles += 1
        if self.b_queue and not snapshot["mem_b_valid"]:
            self.b_wait_cycles += 1

        r_payload = (snapshot["mem_r_data"], snapshot["mem_r_resp"],
                     snapshot["mem_r_last"], snapshot["mem_r_id"])
        previous_r = self._prev_response_stall["r"]
        if previous_r is not None and (not snapshot["mem_r_valid"] or r_payload != previous_r):
            raise AxiProtocolError("R response changed before its handshake")
        self._prev_response_stall["r"] = (
            r_payload if snapshot["mem_r_valid"] and not snapshot["mem_r_ready"] else None
        )
        if snapshot["mem_r_valid"] and not snapshot["mem_r_ready"]:
            self.r_stall_cycles += 1
        if self.ar_queue and not snapshot["mem_r_valid"]:
            self.r_wait_cycles += 1

    def accept(self, snapshot: dict[str, int]) -> None:
        """Apply handshakes sampled before the current rising edge."""
        if snapshot["mem_b_valid"] and snapshot["mem_b_ready"]:
            self.b_queue.popleft()
            self.b_cycles.append(self.cycle)

        if snapshot["mem_r_valid"] and snapshot["mem_r_ready"]:
            assert self.r_active is not None
            self.r_cycles.append(self.cycle)
            self.r_active["index"] += 1
            self.r_current = None
            if snapshot["mem_r_last"]:
                self.ar_queue.popleft()
                self.r_active = None

        if snapshot["mem_aw_valid"] and snapshot["mem_aw_ready"]:
            addr = snapshot["mem_aw_addr"]
            length = snapshot["mem_aw_len"] + 1
            self._validate_burst(addr, length, snapshot["mem_aw_size"],
                                 snapshot["mem_aw_burst"], "AW")
            self.aw_cycles.append(self.cycle)
            self.aw_queue.append({
                "addr": addr,
                "beats": length,
                "size": snapshot["mem_aw_size"],
                "burst": snapshot["mem_aw_burst"],
                "id": snapshot["mem_aw_id"],
                "write_index": len(self.write_transactions),
            })

        if snapshot["mem_w_valid"] and snapshot["mem_w_ready"]:
            data = snapshot["mem_w_data"]
            strobe = snapshot["mem_w_strb"]
            if data < 0 or data >= (1 << self.data_width):
                raise AxiProtocolError(f"W data exceeds {self.data_width}-bit bus")
            if strobe != self.full_strobe:
                raise AxiProtocolError(f"W strobe 0x{strobe:x} is not full 0x{self.full_strobe:x}")
            if not self.aw_queue:
                self.w_before_aw_beats += 1
            self.w_cycles.append(self.cycle)
            self.w_queue.append({"data": data, "strb": strobe, "last": snapshot["mem_w_last"]})

        if snapshot["mem_ar_valid"] and snapshot["mem_ar_ready"]:
            addr = snapshot["mem_ar_addr"]
            length = snapshot["mem_ar_len"] + 1
            self._validate_burst(addr, length, snapshot["mem_ar_size"],
                                 snapshot["mem_ar_burst"], "AR")
            index = len(self.read_transactions)
            delay = self.random.randint(*self.r_delay)
            transaction = {
                "addr": addr,
                "beats": length,
                "size": snapshot["mem_ar_size"],
                "burst": snapshot["mem_ar_burst"],
                "id": snapshot["mem_ar_id"],
                "index": 0,
                "resp": 2 if index in self.read_resp_errors_at else 0,
                "due": self.cycle + delay + 1,
            }
            self.read_transactions.append({key: value for key, value in transaction.items()
                                           if key not in ("index", "due")})
            self.ar_queue.append(transaction)
            self.ar_cycles.append(self.cycle)

        self._complete_writes()
        self.cycle += 1

    def _complete_writes(self) -> None:
        while self.aw_queue and len(self.w_queue) >= self.aw_queue[0]["beats"]:
            address = self.aw_queue[0]["addr"]
            count = self.aw_queue[0]["beats"]
            aw = self.aw_queue.popleft()
            beats = [self.w_queue.popleft() for _ in range(count)]
            for index, beat in enumerate(beats):
                expected_last = int(index == count - 1)
                if beat["last"] != expected_last:
                    raise AxiProtocolError(
                        f"WLAST={beat['last']} on beat {index + 1}/{count} at 0x{address:x}"
                    )
                beat_addr = address + index * self.data_bytes
                for lane in range(self.data_bytes):
                    if beat["strb"] & (1 << lane):
                        self.bytes[beat_addr + lane] = (beat["data"] >> (8 * lane)) & 0xFF

            write_index = len(self.write_transactions)
            self.write_transactions.append({
                "addr": address,
                "beats": count,
                "size": aw["size"],
                "burst": aw["burst"],
                "id": aw["id"],
                "data": [beat["data"] for beat in beats],
                "strobes": [beat["strb"] for beat in beats],
                "bresp": 2 if write_index in self.write_resp_errors_at else 0,
                "b_id": ((aw["id"] + 1) & ((1 << self._aw_id_width) - 1)
                         if write_index in self.write_id_errors_at else aw["id"]),
            })
            delay = self.random.randint(*self.b_delay)
            self.b_queue.append({
                "resp": self.write_transactions[-1]["bresp"],
                "id": self.write_transactions[-1]["b_id"],
                "due": self.cycle + delay + 1,
            })

    def _validate_burst(self, address: int, beats: int, size: int, burst: int, channel: str) -> None:
        if burst != 1:
            raise AxiProtocolError(f"{channel} BURST={burst}, expected INCR (1)")
        if size != self.data_bytes.bit_length() - 1:
            raise AxiProtocolError(f"{channel} SIZE={size}, expected {self.data_bytes.bit_length() - 1}")
        if address % self.data_bytes:
            raise AxiProtocolError(f"{channel} address 0x{address:x} is not beat aligned")
        length_bytes = beats * self.data_bytes
        self._check_span(address, length_bytes, channel)
        if address // 4096 != (address + length_bytes - 1) // 4096:
            raise AxiProtocolError(f"{channel} burst at 0x{address:x} crosses a 4 KiB boundary")

    def _check_span(self, address: int, length: int, operation: str) -> None:
        if address < 0 or length < 0 or address + length > self.aperture_bytes:
            raise AxiProtocolError(
                f"{operation} span [0x{address:x}, 0x{address + length:x}) "
                f"is outside [0, 0x{self.aperture_bytes:x})"
            )
