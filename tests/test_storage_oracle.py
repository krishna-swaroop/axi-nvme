import json
from pathlib import Path

import pytest

from models.storage_oracle import StorageCommandError, StorageOracle, UINT32_MAX, UINT64_MAX


def _oracle(*, lba_bytes=512, nsze=16, host_limit=32768, drive_limit=None):
    return StorageOracle(
        nsid=7,
        nsze=nsze,
        lba_bytes=lba_bytes,
        host_command_byte_limit=host_limit,
        drive_byte_limit=drive_limit,
    )


def _arbitrary_bytes(length, seed):
    return bytes((seed + index * 73 + index // 11) & 0xFF for index in range(length))


@pytest.mark.parametrize("lba_bytes", [512, 4096])
def test_round_trip_is_byte_exact_for_supported_lba_sizes(lba_bytes):
    oracle = _oracle(lba_bytes=lba_bytes, host_limit=4 * lba_bytes)
    payload = _arbitrary_bytes(3 * lba_bytes, seed=0x39)

    oracle.write(7, 2, 3, payload)

    assert oracle.read(7, 2, 3) == payload
    assert oracle.read(7, 0, 2) == bytes(2 * lba_bytes)


def test_synthetic_capture_profile_geometry_is_explicit_and_simulation_only():
    profile_path = Path(__file__).resolve().parents[1] / "config/profiles/synthetic_capture_v1.json"
    profile = json.loads(profile_path.read_text(encoding="utf-8"))
    namespace = profile["namespace"]
    oracle = StorageOracle(
        nsid=namespace["nsid"],
        nsze=namespace["nsze"],
        lba_bytes=namespace["lba_bytes"],
        host_command_byte_limit=namespace["host_max_command_bytes"],
        drive_byte_limit=namespace["drive_max_command_bytes"],
    )

    data_width = profile["data_width"]
    chunk_beats = profile["chunk_beats"]
    assert profile["simulation_only"] is True
    assert type(data_width) is int and data_width > 0 and data_width % 8 == 0
    assert type(chunk_beats) is int and chunk_beats > 0
    chunk_bytes = (data_width // 8) * chunk_beats
    assert chunk_bytes % oracle.lba_bytes == 0
    assert chunk_bytes <= oracle.host_command_byte_limit
    assert oracle.drive_byte_limit is None  # The synthetic profile specifies no known drive limit.


@pytest.mark.parametrize(
    "geometry",
    [
        {"nsid": True},
        {"nsze": True},
        {"lba_bytes": True},
        {"host_command_byte_limit": True},
        {"drive_byte_limit": True},
        {"nsid": 0},
        {"nsze": 0},
        {"lba_bytes": 1024},
        {"host_command_byte_limit": 0},
        {"drive_byte_limit": 0},
        {"nsid": UINT32_MAX + 1},
        {"nsze": UINT64_MAX + 1},
        {"host_command_byte_limit": UINT64_MAX + 1},
        {"drive_byte_limit": UINT64_MAX + 1},
    ],
)
def test_invalid_geometry_is_rejected(geometry):
    values = {
        "nsid": 7,
        "nsze": 16,
        "lba_bytes": 512,
        "host_command_byte_limit": 4096,
        "drive_byte_limit": 4096,
    }
    values.update(geometry)
    with pytest.raises(StorageCommandError):
        StorageOracle(**values)


def test_namespace_and_uint64_boundaries_are_checked_before_access():
    oracle = _oracle(nsze=4, host_limit=4096)
    last_block = _arbitrary_bytes(512, 0xA1)

    assert oracle.read(7, 3, 1) == bytes(512)
    oracle.write(7, 3, 1, last_block)
    assert oracle.read(7, 3, 1) == last_block
    with pytest.raises(StorageCommandError, match="unsupported nsid"):
        oracle.read(8, 3, 1)
    with pytest.raises(StorageCommandError, match="non-negative"):
        oracle.read(7, -1, 1)
    with pytest.raises(StorageCommandError, match="namespace size"):
        oracle.read(7, 4, 1)
    with pytest.raises(StorageCommandError, match="namespace size"):
        oracle.read(7, 3, 2)
    with pytest.raises(StorageCommandError, match="64-bit LBA space"):
        oracle.read(7, UINT64_MAX, 2)
    assert oracle.read(7, 3, 1) == last_block


def test_nsid_upper_boundary_is_supported_and_nlb_overflow_fails_before_payload_check():
    boundary = StorageOracle(
        nsid=UINT32_MAX,
        nsze=1,
        lba_bytes=512,
        host_command_byte_limit=UINT64_MAX,
    )
    assert boundary.read(UINT32_MAX, 0, 1) == bytes(512)
    with pytest.raises(StorageCommandError, match="32-bit NSID"):
        boundary.read(UINT32_MAX + 1, 0, 1)

    too_many_blocks = UINT32_MAX + 1
    large_namespace = StorageOracle(
        nsid=7,
        nsze=too_many_blocks + 1,
        lba_bytes=512,
        host_command_byte_limit=UINT64_MAX,
    )
    with pytest.raises(StorageCommandError, match="32-bit NLB"):
        large_namespace.write(7, 0, too_many_blocks, b"")


@pytest.mark.parametrize("fields", [(True, 0, 1), (7, True, 1), (7, 0, True), (7, 0, 0)])
def test_invalid_integer_command_fields_are_rejected(fields):
    oracle = _oracle()
    with pytest.raises(StorageCommandError):
        oracle.read(*fields)


@pytest.mark.parametrize(
    "host_limit,drive_limit,expected_error",
    [(512, None, "host limit"), (1024, 512, "drive limit")],
)
def test_command_limits_reject_without_mutating_storage(host_limit, drive_limit, expected_error):
    oracle = _oracle(host_limit=host_limit, drive_limit=drive_limit)
    original = _arbitrary_bytes(512, 0xC4)
    replacement = _arbitrary_bytes(1024, 0x12)
    oracle.write(7, 0, 1, original)
    before = oracle.read(7, 0, 1) + oracle.read(7, 1, 1)

    with pytest.raises(StorageCommandError, match=expected_error):
        oracle.write(7, 0, 2, replacement)

    assert oracle.read(7, 0, 1) + oracle.read(7, 1, 1) == before


def test_payload_and_fua_validation_reject_before_any_block_is_written():
    oracle = _oracle()
    before = oracle.read(7, 0, 2)
    with pytest.raises(StorageCommandError, match="payload length"):
        oracle.write(7, 0, 2, bytes(1023))
    with pytest.raises(StorageCommandError, match="bytes-like"):
        oracle.write(7, 0, 1, 512)
    with pytest.raises(StorageCommandError, match="fua must be a bool"):
        oracle.write(7, 0, 1, bytes(512), fua=1)
    assert oracle.read(7, 0, 2) == before


def test_volatile_writes_flush_fua_and_power_loss_have_explicit_model_semantics():
    oracle = _oracle()
    durable = _arbitrary_bytes(512, 0x11)
    volatile = _arbitrary_bytes(512, 0x77)
    other_volatile = _arbitrary_bytes(512, 0xD3)
    fua_data = _arbitrary_bytes(512, 0x4B)

    oracle.write(7, 0, 1, durable, fua=True)
    # A volatile zero write must hide old durable data until lost or flushed.
    oracle.write(7, 0, 1, bytes(512))
    assert oracle.read(7, 0, 1) == bytes(512)
    oracle.power_loss()
    assert oracle.read(7, 0, 1) == durable

    oracle.write(7, 0, 1, volatile)
    oracle.write(7, 1, 1, other_volatile)
    assert oracle.read(7, 0, 2) == volatile + other_volatile
    oracle.flush(7)
    oracle.power_loss()
    assert oracle.read(7, 0, 2) == volatile + other_volatile

    oracle.write(7, 0, 1, durable)
    oracle.write(7, 0, 1, fua_data, fua=True)
    assert oracle.read(7, 0, 1) == fua_data
    oracle.power_loss()
    assert oracle.read(7, 0, 1) == fua_data


def test_fua_does_not_flush_unrelated_volatile_blocks_and_input_is_copied():
    oracle = _oracle()
    mutable_payload = bytearray(_arbitrary_bytes(512, 0x21))
    original_payload = bytes(mutable_payload)
    unrelated_volatile = _arbitrary_bytes(512, 0x96)
    fua_payload = _arbitrary_bytes(512, 0xE2)

    oracle.write(7, 0, 1, mutable_payload)
    mutable_payload[:] = bytes(512)
    oracle.write(7, 1, 1, unrelated_volatile)
    oracle.write(7, 2, 1, fua_payload, fua=True)
    assert oracle.read(7, 0, 3) == original_payload + unrelated_volatile + fua_payload

    oracle.power_loss()
    assert oracle.read(7, 0, 1) == bytes(512)
    assert oracle.read(7, 1, 1) == bytes(512)
    assert oracle.read(7, 2, 1) == fua_payload


def test_invalid_flush_does_not_persist_volatile_data():
    oracle = _oracle()
    volatile = _arbitrary_bytes(512, 0x42)
    oracle.write(7, 0, 1, volatile)

    with pytest.raises(StorageCommandError, match="unsupported nsid"):
        oracle.flush(8)
    oracle.power_loss()

    assert oracle.read(7, 0, 1) == bytes(512)
