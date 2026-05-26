from __future__ import annotations

import hashlib
import math
import struct
import zlib
from dataclasses import dataclass
from pathlib import Path

PROTOCOL_VERSION = 1
PACKET_MAGIC = b"SFV1"
PACKET_HEADER = struct.Struct(">4sB16s8sQ32sIIHI")
HEADER_SIZE = PACKET_HEADER.size
DEFAULT_CHUNK_SIZE = 640


def _digest_name(file_name: str) -> bytes:
    return hashlib.sha256(file_name.encode("utf-8")).digest()[:8]


def _build_file_id(file_sha256: bytes, file_size: int) -> bytes:
    h = hashlib.sha256()
    h.update(file_sha256)
    h.update(struct.pack(">Q", file_size))
    return h.digest()[:16]


@dataclass(frozen=True)
class Packet:
    version: int
    file_id: bytes
    file_name_digest: bytes
    file_size: int
    file_sha256: bytes
    chunk_index: int
    total_chunks: int
    payload_crc32: int
    payload: bytes

    def to_bytes(self) -> bytes:
        return PACKET_HEADER.pack(
            PACKET_MAGIC,
            self.version,
            self.file_id,
            self.file_name_digest,
            self.file_size,
            self.file_sha256,
            self.chunk_index,
            self.total_chunks,
            len(self.payload),
            self.payload_crc32,
        ) + self.payload


def packet_from_bytes(blob: bytes) -> Packet:
    if len(blob) < HEADER_SIZE:
        raise ValueError("Packet too short")

    (
        magic,
        version,
        file_id,
        file_name_digest,
        file_size,
        file_sha256,
        chunk_index,
        total_chunks,
        payload_size,
        payload_crc32,
    ) = PACKET_HEADER.unpack(blob[:HEADER_SIZE])

    if magic != PACKET_MAGIC:
        raise ValueError("Invalid packet magic")

    payload = blob[HEADER_SIZE : HEADER_SIZE + payload_size]
    if len(payload) != payload_size:
        raise ValueError("Incomplete packet payload")
    if zlib.crc32(payload) & 0xFFFFFFFF != payload_crc32:
        raise ValueError("Payload CRC mismatch")

    return Packet(
        version=version,
        file_id=file_id,
        file_name_digest=file_name_digest,
        file_size=file_size,
        file_sha256=file_sha256,
        chunk_index=chunk_index,
        total_chunks=total_chunks,
        payload_crc32=payload_crc32,
        payload=payload,
    )


def build_packets_from_bytes(
    file_name: str,
    payload: bytes,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
) -> list[Packet]:
    if chunk_size <= 0:
        raise ValueError("chunk_size must be positive")

    file_sha256 = hashlib.sha256(payload).digest()
    file_id = _build_file_id(file_sha256, len(payload))
    file_name_digest = _digest_name(file_name)
    total_chunks = max(1, math.ceil(len(payload) / chunk_size))
    packets: list[Packet] = []

    for chunk_index in range(total_chunks):
        start = chunk_index * chunk_size
        end = start + chunk_size
        chunk = payload[start:end]
        packets.append(
            Packet(
                version=PROTOCOL_VERSION,
                file_id=file_id,
                file_name_digest=file_name_digest,
                file_size=len(payload),
                file_sha256=file_sha256,
                chunk_index=chunk_index,
                total_chunks=total_chunks,
                payload_crc32=zlib.crc32(chunk) & 0xFFFFFFFF,
                payload=chunk,
            )
        )

    return packets


def build_packets_from_file(path: Path, chunk_size: int = DEFAULT_CHUNK_SIZE) -> list[Packet]:
    return build_packets_from_bytes(path.name, path.read_bytes(), chunk_size=chunk_size)
