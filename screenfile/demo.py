from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from screenfile.cli import decode_video_to_file, encode_file_to_video


@dataclass(frozen=True)
class DemoResult:
    source_path: Path
    video_path: Path
    restored_path: Path


def build_demo_payload(size: int) -> bytes:
    pattern = b"screenfile-demo-payload-"
    repeat = (size // len(pattern)) + 1
    return (pattern * repeat)[:size]


def run_demo_roundtrip(
    output_dir: Path,
    *,
    payload_size: int = 64 * 1024,
    repeat: int = 3,
    fps: int = 8,
) -> DemoResult:
    output_dir.mkdir(parents=True, exist_ok=True)
    source_path = output_dir / "demo-source.bin"
    video_path = output_dir / "demo-transfer.mp4"
    restored_path = output_dir / "demo-restored.bin"

    source_path.write_bytes(build_demo_payload(payload_size))
    encode_file_to_video(source_path, video_path, repeat=repeat, fps=fps, skip_confirmation=True)
    decode_video_to_file(video_path, restored_path)
    return DemoResult(source_path=source_path, video_path=video_path, restored_path=restored_path)
