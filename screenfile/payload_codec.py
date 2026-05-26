from __future__ import annotations

import gzip
import hashlib
import struct
from dataclasses import dataclass

try:
    import compression.zstd as zstd
except ImportError:  # pragma: no cover
    zstd = None

TRANSPORT_MAGIC = b"SFP1"
TRANSPORT_HEADER = struct.Struct(">4sBBQH32s")
COMPRESSION_IDS = {"none": 0, "gzip": 1, "zstd": 2}
COMPRESSION_NAMES = {value: key for key, value in COMPRESSION_IDS.items()}


@dataclass(frozen=True)
class DecodedPayload:
    original_name: str
    original_bytes: bytes
    compression: str
    encoded_size: int


def _compress(payload: bytes, compression: str) -> bytes:
    if compression == "none":
        return payload
    if compression == "gzip":
        return gzip.compress(payload, compresslevel=9)
    if compression == "zstd":
        if zstd is None:
            raise RuntimeError("zstd compression is unavailable in this Python runtime")
        return zstd.compress(payload, level=10)
    raise ValueError(f"Unsupported compression: {compression}")


def _decompress(payload: bytes, compression: str) -> bytes:
    if compression == "none":
        return payload
    if compression == "gzip":
        return gzip.decompress(payload)
    if compression == "zstd":
        if zstd is None:
            raise RuntimeError("zstd decompression is unavailable in this Python runtime")
        return zstd.decompress(payload)
    raise ValueError(f"Unsupported compression: {compression}")


def encode_transport_payload(file_name: str, raw_bytes: bytes, compression: str = "zstd") -> bytes:
    if compression not in COMPRESSION_IDS:
        raise ValueError(f"Unsupported compression: {compression}")

    compressed = _compress(raw_bytes, compression)
    name_bytes = file_name.encode("utf-8")
    if len(name_bytes) > 65535:
        raise ValueError("File name is too long")

    header = TRANSPORT_HEADER.pack(
        TRANSPORT_MAGIC,
        1,
        COMPRESSION_IDS[compression],
        len(raw_bytes),
        len(name_bytes),
        hashlib.sha256(raw_bytes).digest(),
    )
    return header + name_bytes + compressed


def decode_transport_payload(blob: bytes) -> DecodedPayload:
    if len(blob) < TRANSPORT_HEADER.size or blob[:4] != TRANSPORT_MAGIC:
        return DecodedPayload(
            original_name="recovered.bin",
            original_bytes=blob,
            compression="none",
            encoded_size=len(blob),
        )

    magic, version, compression_id, original_size, name_len, digest = TRANSPORT_HEADER.unpack(
        blob[: TRANSPORT_HEADER.size]
    )
    if magic != TRANSPORT_MAGIC or version != 1:
        raise ValueError("Unsupported transport payload format")

    start = TRANSPORT_HEADER.size
    end = start + name_len
    name_bytes = blob[start:end]
    compressed = blob[end:]
    compression = COMPRESSION_NAMES.get(compression_id)
    if compression is None:
        raise ValueError("Unknown compression id")

    original = _decompress(compressed, compression)
    if len(original) != original_size:
        raise ValueError("Decoded payload size mismatch")
    if hashlib.sha256(original).digest() != digest:
        raise ValueError("Decoded payload hash mismatch")

    return DecodedPayload(
        original_name=name_bytes.decode("utf-8"),
        original_bytes=original,
        compression=compression,
        encoded_size=len(blob),
    )
