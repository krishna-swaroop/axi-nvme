"""Sparse byte-exact namespace model for synthetic contract tests.

This oracle defines zero as the initial contents of every unwritten LBA. That is
only a model initialization rule; it is not an NVMe or SSD guarantee. Likewise,
the configured byte limits and volatile-cache/FUA/Flush behavior are explicit
synthetic scenario semantics, not claims about any real drive's limits or
durability behavior. A ``None`` drive limit means the scenario has no known
drive limit; it does not mean a real SSD has no limit.

The model is intentionally independent of DUT, LiteNVMe and LitePCIe command,
queue, DMA and PRP code. Its storage is keyed directly by namespace LBA.
"""

from __future__ import annotations


UINT32_MAX = (1 << 32) - 1
UINT64_MAX = (1 << 64) - 1
SUPPORTED_LBA_BYTES = (512, 4096)


class StorageCommandError(ValueError):
    """Raised when a synthetic namespace command violates its declared contract."""


def _require_int(name: str, value: object) -> int:
    if type(value) is not int:
        raise StorageCommandError(f"{name} must be an integer; bool is not accepted")
    return value


def _require_positive_int(name: str, value: object) -> int:
    result = _require_int(name, value)
    if result <= 0:
        raise StorageCommandError(f"{name} must be positive")
    return result


class StorageOracle:
    """Independent sparse byte store with explicit geometry and cache semantics.

    Args:
        nsid: Positive namespace identifier accepted by this model.
        nsze: Positive namespace size in logical blocks, limited to uint64.
        lba_bytes: Supported logical block size, exactly 512 or 4096 bytes.
        host_command_byte_limit: Required positive maximum payload bytes for one
            host command in this synthetic scenario.
        drive_byte_limit: Optional positive per-command drive limit. ``None``
            means no drive limit is known in the scenario, not that real drives
            are unlimited.

    Ordinary writes update volatile state and are visible to reads immediately.
    FUA writes update durable state for their blocks. ``flush`` persists all
    current volatile writes, while ``power_loss`` discards only volatile state.
    These are model rules, not assertions about an unqualified SSD.
    """

    def __init__(
        self,
        *,
        nsid: int,
        nsze: int,
        lba_bytes: int,
        host_command_byte_limit: int,
        drive_byte_limit: int | None = None,
    ) -> None:
        self.nsid = _require_positive_int("nsid", nsid)
        if self.nsid > UINT32_MAX:
            raise StorageCommandError("nsid exceeds the 32-bit NSID range")
        self.nsze = _require_positive_int("nsze", nsze)
        if self.nsze > UINT64_MAX:
            raise StorageCommandError("nsze exceeds the 64-bit namespace-size range")
        self.lba_bytes = _require_int("lba_bytes", lba_bytes)
        if self.lba_bytes not in SUPPORTED_LBA_BYTES:
            raise StorageCommandError("lba_bytes must be 512 or 4096")
        self.host_command_byte_limit = _require_positive_int(
            "host_command_byte_limit", host_command_byte_limit,
        )
        if self.host_command_byte_limit > UINT64_MAX:
            raise StorageCommandError("host_command_byte_limit exceeds the 64-bit payload-byte range")
        if drive_byte_limit is not None:
            drive_byte_limit = _require_positive_int("drive_byte_limit", drive_byte_limit)
            if drive_byte_limit > UINT64_MAX:
                raise StorageCommandError("drive_byte_limit exceeds the 64-bit payload-byte range")
        self.drive_byte_limit = drive_byte_limit

        # Omitted entries read as zero by this synthetic model's initial-state rule.
        self._durable_blocks: dict[int, bytes] = {}
        # None is a sparse zero-block tombstone that hides an older durable block.
        self._volatile_blocks: dict[int, bytes | None] = {}

    def _validate_nsid(self, nsid: int) -> None:
        nsid = _require_positive_int("nsid", nsid)
        if nsid > UINT32_MAX:
            raise StorageCommandError("nsid exceeds the 32-bit NSID range")
        if nsid != self.nsid:
            raise StorageCommandError(f"unsupported nsid {nsid}; model serves nsid {self.nsid}")

    def _validate_request(self, nsid: int, slba: int, block_count: int) -> int:
        self._validate_nsid(nsid)
        slba = _require_int("slba", slba)
        block_count = _require_positive_int("block_count", block_count)
        if block_count > UINT32_MAX:
            raise StorageCommandError("block_count exceeds the 32-bit NLB range")
        if slba < 0:
            raise StorageCommandError("slba must be non-negative")
        if slba > UINT64_MAX or block_count - 1 > UINT64_MAX - slba:
            raise StorageCommandError("LBA range overflows the 64-bit LBA space")

        last_lba = slba + block_count - 1
        if last_lba >= self.nsze:
            raise StorageCommandError(
                f"LBA range [{slba}, {last_lba}] exceeds namespace size {self.nsze}"
            )

        command_bytes = block_count * self.lba_bytes
        if command_bytes > UINT64_MAX:
            raise StorageCommandError("command payload byte count exceeds the 64-bit range")
        if command_bytes > self.host_command_byte_limit:
            raise StorageCommandError(
                f"command payload {command_bytes} exceeds host limit {self.host_command_byte_limit}"
            )
        if self.drive_byte_limit is not None and command_bytes > self.drive_byte_limit:
            raise StorageCommandError(
                f"command payload {command_bytes} exceeds scenario drive limit {self.drive_byte_limit}"
            )
        return command_bytes

    def _payload_bytes(self, payload: bytes | bytearray | memoryview, expected: int) -> bytes:
        if not isinstance(payload, (bytes, bytearray, memoryview)):
            raise StorageCommandError("payload must be bytes-like")
        data = bytes(payload)
        if len(data) != expected:
            raise StorageCommandError(
                f"payload length {len(data)} does not match required length {expected}"
            )
        return data

    def _store_block(self, destination: dict[int, bytes], lba: int, data: bytes) -> None:
        if data == bytes(self.lba_bytes):
            destination.pop(lba, None)
        else:
            destination[lba] = data

    def read(self, nsid: int, slba: int, block_count: int) -> bytes:
        """Read exact bytes from the volatile overlay, durable state, or zero state."""
        self._validate_request(nsid, slba, block_count)
        result = bytearray(block_count * self.lba_bytes)
        for offset in range(block_count):
            lba = slba + offset
            data = (
                self._volatile_blocks[lba]
                if lba in self._volatile_blocks
                else self._durable_blocks.get(lba)
            )
            if data is not None:
                begin = offset * self.lba_bytes
                result[begin:begin + self.lba_bytes] = data
        return bytes(result)

    def write(
        self,
        nsid: int,
        slba: int,
        block_count: int,
        payload: bytes | bytearray | memoryview,
        *,
        fua: bool = False,
    ) -> None:
        """Write exact full-block bytes; validate the whole command before mutation."""
        expected = self._validate_request(nsid, slba, block_count)
        if not isinstance(fua, bool):
            raise StorageCommandError("fua must be a bool")
        data = self._payload_bytes(payload, expected)
        blocks = [
            data[offset:offset + self.lba_bytes]
            for offset in range(0, expected, self.lba_bytes)
        ]

        for offset, block in enumerate(blocks):
            lba = slba + offset
            if fua:
                self._store_block(self._durable_blocks, lba, block)
                # Replace any older volatile overlay with this command's durable data.
                self._volatile_blocks.pop(lba, None)
            else:
                # Preserve a zero tombstone to shadow a prior durable nonzero block.
                self._volatile_blocks[lba] = None if block == bytes(self.lba_bytes) else block

    def flush(self, nsid: int) -> None:
        """Persist every volatile block in this synthetic namespace model."""
        self._validate_nsid(nsid)
        for lba, data in self._volatile_blocks.items():
            if data is None:
                self._durable_blocks.pop(lba, None)
            else:
                self._store_block(self._durable_blocks, lba, data)
        self._volatile_blocks.clear()

    def power_loss(self) -> None:
        """Discard volatile-only writes while retaining synthetic durable state."""
        self._volatile_blocks.clear()
