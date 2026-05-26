from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path

from screenfile.chunking import Packet


def format_missing_ranges(indices: list[int]) -> str:
    if not indices:
        return ""

    ranges: list[str] = []
    start = prev = indices[0]
    for value in indices[1:]:
        if value == prev + 1:
            prev = value
            continue
        ranges.append(f"{start}-{prev}" if start != prev else str(start))
        start = prev = value
    ranges.append(f"{start}-{prev}" if start != prev else str(start))
    return ", ".join(ranges)


@dataclass
class RecoverySession:
    file_id: bytes | None = None
    file_sha256: bytes | None = None
    file_size: int | None = None
    total_chunks: int | None = None
    chunks: dict[int, bytes] = field(default_factory=dict)
    duplicate_chunks: int = 0

    def add_packet(self, packet: Packet) -> None:
        if self.file_id is None:
            self.file_id = packet.file_id
            self.file_sha256 = packet.file_sha256
            self.file_size = packet.file_size
            self.total_chunks = packet.total_chunks
        elif (
            packet.file_id != self.file_id
            or packet.file_sha256 != self.file_sha256
            or packet.file_size != self.file_size
            or packet.total_chunks != self.total_chunks
        ):
            raise ValueError("Packet does not belong to the current recovery session")

        if packet.chunk_index in self.chunks:
            self.duplicate_chunks += 1
            return
        self.chunks[packet.chunk_index] = packet.payload

    @property
    def is_complete(self) -> bool:
        return self.total_chunks is not None and len(self.chunks) == self.total_chunks

    def missing_chunks(self) -> list[int]:
        if self.total_chunks is None:
            return []
        return [index for index in range(self.total_chunks) if index not in self.chunks]

    def assemble_bytes(self) -> bytes:
        if not self.is_complete:
            missing = self.missing_chunks()
            raise RuntimeError(f"Missing chunks: {format_missing_ranges(missing)}")

        ordered = b"".join(self.chunks[index] for index in range(self.total_chunks or 0))
        if self.file_size is not None:
            ordered = ordered[: self.file_size]
        digest = hashlib.sha256(ordered).digest()
        if digest != self.file_sha256:
            raise RuntimeError("Recovered file hash mismatch")
        return ordered

    def write_file(self, output_path: Path) -> None:
        output_path.write_bytes(self.assemble_bytes())
