"""SLCF Binary Codec — struct-packed encoding/decoding for v3 format.

Provides binary serialization for all SLCF field types, aligned with
the SLCFv1.1 Wire Protocol spec. Zero translation cost between
on-disk storage and wire transmission.

Encoding rules:
    - float64: IEEE 754 double, 8 bytes, big-endian
    - uint8/uint16/uint32/uint64: big-endian (network byte order)
    - Enums: dictionary-encoded uint8_t
    - Strings: uint16_t length + UTF-8 bytes
    - Byte arrays: uint32_t length + raw bytes
    - Lists: uint32_t count + encoded elements
    - Page index entries: fixed 20 bytes each
"""

from __future__ import annotations

import struct
import zlib
from dataclasses import dataclass
from enum import IntEnum
from typing import Any

from cascade_guard.slcf.format import (
    FlowState,
    PageIndexEntry,
    SLCFFooter,
    SLCFLayer,
)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Wire-compatible page header size (42 bytes without checksum, 46 with)
WIRE_HEADER_SIZE = 42
WIRE_HEADER_SIZE_WITH_CHECKSUM = 46

# SLCF v3 version number
SLCF_V3_VERSION = 3

# Page type enum (matches SLCFv1.1 wire protocol)
class PageType(IntEnum):
    DATA_PAGE = 0
    DICTIONARY_PAGE = 1
    RARE_VALUE_PAGE = 2
    INDEX_PAGE = 3


# Flow state encoding dictionary
_FLOW_STATE_ENCODE = {
    "laminar": 0,
    "turbulent": 1,
    "retreat": 2,
}
_FLOW_STATE_DECODE = {v: k for k, v in _FLOW_STATE_ENCODE.items()}

# Layer encoding (already IntEnum, but explicit for wire format)
_LAYER_ENCODE = {layer: layer.value for layer in SLCFLayer}


# ---------------------------------------------------------------------------
# Wire Page Header
# ---------------------------------------------------------------------------

@dataclass
class WirePageHeader:
    """42-byte wire-compatible page header (SLCFv1.1 spec).

    All multibyte integers in network byte order (big-endian).
    Packed with no padding.
    """

    magic: int = 0x534C4346  # 'SLCF'
    version: int = 1  # Protocol version
    flags: int = 0  # Bitfield: first_page(1), last_page(1), has_checksum(1), reserved(5)
    file_id: int = 0  # SHA-256 truncated to uint64
    page_seq: int = 0  # Global sequence number
    row_group_id: int = 0  # Row group index
    column_id: int = 0  # Column index within row group
    page_type: int = PageType.DATA_PAGE  # SLCF page type
    decompressed_size: int = 0  # Uncompressed payload size
    compressed_size: int = 0  # Compressed payload size (0 = uncompressed)
    checksum: int = 0  # CRC32C (optional, if flags.has_checksum)

    # Struct format: !IBBQIIIB3xIII (42 bytes, checksum always present)
    # magic(4) + version(1) + flags(1) + file_id(8) + page_seq(4) +
    # row_group_id(4) + column_id(4) + page_type(1) + reserved(3) +
    # decompressed_size(4) + compressed_size(4) + checksum(4) = 42
    _PACK_FORMAT = "!IBBQIIIB3xIII"

    def to_bytes(self) -> bytes:
        """Serialize header to binary (always 42 bytes)."""
        return struct.pack(
            self._PACK_FORMAT,
            self.magic, self.version, self.flags, self.file_id,
            self.page_seq, self.row_group_id, self.column_id,
            self.page_type, self.decompressed_size, self.compressed_size,
            self.checksum,
        )

    @classmethod
    def from_bytes(cls, data: bytes) -> "WirePageHeader":
        """Deserialize header from binary (42 bytes)."""
        values = struct.unpack(cls._PACK_FORMAT, data[:42])
        return cls(
            magic=values[0], version=values[1], flags=values[2],
            file_id=values[3], page_seq=values[4], row_group_id=values[5],
            column_id=values[6], page_type=values[7],
            decompressed_size=values[8], compressed_size=values[9],
            checksum=values[10],
        )

    @property
    def total_size(self) -> int:
        """Total header size including optional checksum."""
        if self.flags & 0x04:
            return WIRE_HEADER_SIZE_WITH_CHECKSUM
        return WIRE_HEADER_SIZE


# ---------------------------------------------------------------------------
# Field Encoders
# ---------------------------------------------------------------------------


def encode_float64(value: float) -> bytes:
    """Encode a float64 as IEEE 754 double, big-endian (8 bytes)."""
    return struct.pack("!d", value)


def decode_float64(data: bytes, offset: int = 0) -> tuple[float, int]:
    """Decode a float64. Returns (value, new_offset)."""
    value = struct.unpack("!d", data[offset:offset + 8])[0]
    return value, offset + 8


def encode_uint8(value: int) -> bytes:
    """Encode uint8 (1 byte)."""
    return struct.pack("!B", value & 0xFF)


def decode_uint8(data: bytes, offset: int = 0) -> tuple[int, int]:
    """Decode uint8. Returns (value, new_offset)."""
    return data[offset], offset + 1


def encode_uint16(value: int) -> bytes:
    """Encode uint16, big-endian (2 bytes)."""
    return struct.pack("!H", value & 0xFFFF)


def decode_uint16(data: bytes, offset: int = 0) -> tuple[int, int]:
    """Decode uint16. Returns (value, new_offset)."""
    value = struct.unpack("!H", data[offset:offset + 2])[0]
    return value, offset + 2


def encode_uint32(value: int) -> bytes:
    """Encode uint32, big-endian (4 bytes)."""
    return struct.pack("!I", value & 0xFFFFFFFF)


def decode_uint32(data: bytes, offset: int = 0) -> tuple[int, int]:
    """Decode uint32. Returns (value, new_offset)."""
    value = struct.unpack("!I", data[offset:offset + 4])[0]
    return value, offset + 4


def encode_uint64(value: int) -> bytes:
    """Encode uint64, big-endian (8 bytes)."""
    return struct.pack("!Q", value & 0xFFFFFFFFFFFFFFFF)


def decode_uint64(data: bytes, offset: int = 0) -> tuple[int, int]:
    """Decode uint64. Returns (value, new_offset)."""
    value = struct.unpack("!Q", data[offset:offset + 8])[0]
    return value, offset + 8


def encode_string(value: str) -> bytes:
    """Encode length-prefixed string: uint16_t length + UTF-8 bytes."""
    encoded = value.encode("utf-8")
    return struct.pack("!H", len(encoded)) + encoded


def decode_string(data: bytes, offset: int = 0) -> tuple[str, int]:
    """Decode length-prefixed string. Returns (value, new_offset)."""
    length = struct.unpack("!H", data[offset:offset + 2])[0]
    offset += 2
    value = data[offset:offset + length].decode("utf-8")
    return value, offset + length


def encode_bytes(value: bytes) -> bytes:
    """Encode length-prefixed byte array: uint32_t length + raw bytes."""
    return struct.pack("!I", len(value)) + value


def decode_bytes(data: bytes, offset: int = 0) -> tuple[bytes, int]:
    """Decode length-prefixed byte array. Returns (value, new_offset)."""
    length = struct.unpack("!I", data[offset:offset + 4])[0]
    offset += 4
    value = data[offset:offset + length]
    return value, offset + length


def encode_flow_state(state: str) -> bytes:
    """Encode flow state as dictionary-encoded uint8."""
    return encode_uint8(_FLOW_STATE_ENCODE.get(state, 0))


def decode_flow_state(data: bytes, offset: int = 0) -> tuple[str, int]:
    """Decode flow state. Returns (value, new_offset)."""
    code, offset = decode_uint8(data, offset)
    return _FLOW_STATE_DECODE.get(code, "laminar"), offset


def compute_crc32c(data: bytes) -> int:
    """Compute CRC32C checksum (IEEE 802.3 compatible via zlib)."""
    return zlib.crc32(data) & 0xFFFFFFFF


# ---------------------------------------------------------------------------
# Footer Binary Encoding
# ---------------------------------------------------------------------------


def encode_footer(footer: SLCFFooter) -> bytes:
    """Encode SLCFFooter to binary format.

    Layout:
        maat_hash (32 bytes, raw)
        sigma_delta (float64, 8 bytes)
        kappa_effective (float64, 8 bytes)
        beta_one (uint32, 4 bytes)
        flow_state (uint8, 1 byte)
        target_size (uint32, 4 bytes)
        actual_size (uint32, 4 bytes)
        page_count (uint32, 4 bytes)
        page_index (page_count × 20 bytes each)
    """
    parts = []

    # Maat hash: fixed 32 bytes (pad if shorter)
    maat_hash = footer.maat_hash
    if len(maat_hash) < 32:
        maat_hash = maat_hash + b"\x00" * (32 - len(maat_hash))
    parts.append(maat_hash[:32])

    # Metrics
    parts.append(encode_float64(footer.sigma_delta))
    parts.append(encode_float64(footer.kappa_effective))
    parts.append(encode_uint32(footer.beta_one))
    parts.append(encode_flow_state(footer.flow_state))
    parts.append(encode_uint32(footer.target_size))
    parts.append(encode_uint32(footer.actual_size))

    # Page index
    parts.append(encode_uint32(len(footer.page_index)))
    for entry in footer.page_index:
        parts.append(encode_page_index_entry(entry))

    return b"".join(parts)


def decode_footer(data: bytes) -> SLCFFooter:
    """Decode SLCFFooter from binary format."""
    offset = 0

    # Maat hash (32 bytes)
    maat_hash = data[offset:offset + 32]
    offset += 32

    # Metrics
    sigma_delta, offset = decode_float64(data, offset)
    kappa_effective, offset = decode_float64(data, offset)
    beta_one, offset = decode_uint32(data, offset)
    flow_state, offset = decode_flow_state(data, offset)
    target_size, offset = decode_uint32(data, offset)
    actual_size, offset = decode_uint32(data, offset)

    # Page index
    page_count, offset = decode_uint32(data, offset)
    page_index = []
    for _ in range(page_count):
        entry, offset = decode_page_index_entry(data, offset)
        page_index.append(entry)

    return SLCFFooter(
        maat_hash=maat_hash,
        sigma_delta=sigma_delta,
        kappa_effective=kappa_effective,
        beta_one=beta_one,
        flow_state=flow_state,
        page_index=page_index,
        target_size=target_size,
        actual_size=actual_size,
    )


# ---------------------------------------------------------------------------
# Page Index Entry Encoding (fixed 20 bytes)
# ---------------------------------------------------------------------------


def encode_page_index_entry(entry: PageIndexEntry) -> bytes:
    """Encode a page index entry to fixed 20 bytes.

    Layout: page_id(4) + layer_id(1) + offset(8) + size(4) + padding(3)
    """
    return struct.pack(
        "!IB Q I 3x",
        entry.page_id,
        int(entry.layer_id),
        entry.offset,
        entry.size,
    )


def decode_page_index_entry(data: bytes, offset: int = 0) -> tuple[PageIndexEntry, int]:
    """Decode a page index entry from 20 bytes."""
    page_id, layer_id, entry_offset, size = struct.unpack(
        "!IB Q I", data[offset:offset + 17]
    )
    # Skip 3 bytes padding
    return PageIndexEntry(
        page_id=page_id,
        layer_id=SLCFLayer(layer_id),
        offset=entry_offset,
        size=size,
    ), offset + 20
