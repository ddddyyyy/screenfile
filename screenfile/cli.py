from __future__ import annotations

import argparse
import cv2
import tempfile
from pathlib import Path

from screenfile import __version__
from screenfile.chunking import DEFAULT_CHUNK_SIZE, build_packets_from_bytes
from screenfile.frame_codec import FRAME_HEIGHT, FRAME_WIDTH, encode_packet_frame, inspect_frame
from screenfile.payload_codec import decode_transport_payload, encode_transport_payload
from screenfile.recovery import RecoverySession, format_missing_ranges
from screenfile.video_io import read_video_frames, write_video


def _format_storage_size(num_bytes: int) -> str:
    gib = 1024**3
    mib = 1024**2
    kib = 1024
    if num_bytes >= gib:
        return f"{num_bytes / gib:.2f} GB"
    if num_bytes >= mib:
        return f"{num_bytes / mib:.2f} MB"
    if num_bytes >= kib:
        return f"{num_bytes / kib:.2f} KB"
    if num_bytes == 1:
        return "1 B"
    if num_bytes == 0:
        return "0 B"
    return f"{num_bytes} B"


def _format_video_size(num_bytes: int) -> str:
    gib = 1024**3
    mib = 1024**2
    if num_bytes >= gib:
        return f"{num_bytes / gib:.2f} GB"
    return f"{num_bytes / mib:.2f} MB"


def _format_storage_size_with_bytes(num_bytes: int) -> str:
    return f"{_format_storage_size(num_bytes)} ({num_bytes:,} B)"


def _build_estimate_sample_frames(packets, *, repeat: int, max_sample_frames: int = 24) -> list:
    total_frames = len(packets) * repeat
    sample_frames = max(1, min(total_frames, max_sample_frames))
    if total_frames <= sample_frames:
        indices = list(range(total_frames))
    else:
        indices = []
        for position in range(sample_frames):
            index = round(position * (total_frames - 1) / (sample_frames - 1))
            if not indices or index != indices[-1]:
                indices.append(index)

    frames = []
    packet_count = len(packets)
    for index in indices:
        packet = packets[index % packet_count]
        frames.append(encode_packet_frame(packet))
    return frames


def _estimate_video_size_bytes(*, sample_frames: list, total_frames: int, fps: int, suffix: str) -> int:
    if not sample_frames:
        raise ValueError("At least one sample frame is required")

    with tempfile.TemporaryDirectory() as tmpdir:
        probe_path = Path(tmpdir) / f"estimate{suffix}"
        write_video(
            probe_path,
            (frame.copy() for frame in sample_frames),
            fps=fps,
            frame_size=(FRAME_WIDTH, FRAME_HEIGHT),
        )
        probe_size = probe_path.stat().st_size
    estimated = int(round(probe_size * (total_frames / len(sample_frames))))
    return max(estimated, probe_size)


def _estimate_for_compression(
    input_file: Path,
    *,
    chunk_size: int,
    repeat: int,
    fps: int,
    compression: str,
    output_suffix: str = ".mp4",
) -> dict[str, float | int | str]:
    original_bytes = input_file.read_bytes()
    transport_payload = encode_transport_payload(input_file.name, original_bytes, compression=compression)
    packets = build_packets_from_bytes(input_file.name, transport_payload, chunk_size=chunk_size)
    frames = len(packets) * repeat
    sample_frames = _build_estimate_sample_frames(packets, repeat=repeat)
    estimated_video_size = _estimate_video_size_bytes(
        sample_frames=sample_frames,
        total_frames=frames,
        fps=fps,
        suffix=output_suffix,
    )
    return {
        "compression": compression,
        "original_bytes": len(original_bytes),
        "original_size_human": _format_storage_size_with_bytes(len(original_bytes)),
        "encoded_bytes": len(transport_payload),
        "encoded_size_human": _format_storage_size_with_bytes(len(transport_payload)),
        "ratio": len(transport_payload) / max(1, len(original_bytes)),
        "chunks": len(packets),
        "frames": frames,
        "duration": frames / fps,
        "estimated_video_size_bytes": estimated_video_size,
        "estimated_video_size_human": _format_video_size(estimated_video_size),
    }


def _print_estimate(summary: dict[str, float | int | str]) -> None:
    print(f"Compression: {summary['compression']}")
    print(f"Original size: {summary['original_size_human']}")
    print(f"Encoded size: {summary['encoded_size_human']}")
    print(f"Compression ratio: {summary['ratio']:.3f}")
    print(f"Chunks: {summary['chunks']}")
    print(f"Frames: {summary['frames']}")
    print(f"Estimated duration: {summary['duration']:.1f}s")
    print(f"Estimated video size: {summary['estimated_video_size_human']}")


def print_estimates(
    input_file: Path,
    *,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
    repeat: int = 3,
    fps: int = 8,
    output_suffix: str = ".mp4",
) -> None:
    for compression in ("none", "gzip", "zstd"):
        _print_estimate(
            _estimate_for_compression(
                input_file,
                chunk_size=chunk_size,
                repeat=repeat,
                fps=fps,
                compression=compression,
                output_suffix=output_suffix,
            )
        )
        print()


def encode_file_to_video(
    input_file: Path,
    output_video: Path,
    *,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
    repeat: int = 3,
    fps: int = 8,
    compression: str = "zstd",
    skip_confirmation: bool = False,
) -> None:
    summary = _estimate_for_compression(
        input_file,
        chunk_size=chunk_size,
        repeat=repeat,
        fps=fps,
        compression=compression,
        output_suffix=output_video.suffix.lower() or ".mp4",
    )
    _print_estimate(summary)
    print(f"Repeat count: {repeat}")
    print("Playback tips: fullscreen, max brightness, avoid window animations.")

    if not skip_confirmation:
        response = input("Proceed with video generation? [y/N]: ").strip().lower()
        print("Proceed with video generation?")
        if response not in {"y", "yes"}:
            print("Cancelled.")
            return

    original_bytes = input_file.read_bytes()
    transport_payload = encode_transport_payload(input_file.name, original_bytes, compression=compression)
    packets = build_packets_from_bytes(input_file.name, transport_payload, chunk_size=chunk_size)

    def iter_frames():
        for _ in range(repeat):
            for packet in packets:
                yield encode_packet_frame(packet)

    write_video(output_video, iter_frames(), fps=fps, frame_size=(FRAME_WIDTH, FRAME_HEIGHT))
    print(f"Wrote video to {output_video}")


def _write_debug_image(path: Path, image) -> None:
    cv2.imwrite(str(path), image)


def _maybe_write_debug_artifacts(
    debug_dir: Path | None,
    *,
    debug_limit: int,
    debug_written: int,
    frame_number: int,
    frame,
    status: str,
    square,
    binary,
) -> int:
    if debug_dir is None or debug_written >= debug_limit:
        return debug_written

    debug_dir.mkdir(parents=True, exist_ok=True)
    stem = f"frame-{frame_number:05d}-{status}"
    _write_debug_image(debug_dir / f"{stem}-input.jpg", frame)
    if square is not None:
        _write_debug_image(debug_dir / f"{stem}-square.png", square)
    if binary is not None:
        _write_debug_image(debug_dir / f"{stem}-binary.png", binary)
    return debug_written + 1


def decode_video_to_file(
    input_video: Path,
    output_file: Path,
    *,
    debug_dir: Path | None = None,
    debug_limit: int = 12,
) -> None:
    session = RecoverySession()
    total_frames = 0
    decoded_frames = 0
    frame_crc_failures = 0
    packet_failures = 0
    no_quad_failures = 0
    debug_written = 0
    first_valid_frame: int | None = None
    last_valid_frame: int | None = None

    for frame in read_video_frames(input_video):
        total_frames += 1
        inspection = inspect_frame(frame)
        packet, status = inspection.packet, inspection.status

        if status != "ok":
            if status == "frame-crc":
                frame_crc_failures += 1
            elif status == "packet-invalid":
                packet_failures += 1
            else:
                no_quad_failures += 1
            debug_written = _maybe_write_debug_artifacts(
                debug_dir,
                debug_limit=debug_limit,
                debug_written=debug_written,
                frame_number=total_frames,
                frame=frame,
                status=status,
                square=inspection.square,
                binary=inspection.binary,
            )
            continue

        assert packet is not None
        try:
            session.add_packet(packet)
        except ValueError:
            packet_failures += 1
            continue
        decoded_frames += 1
        if first_valid_frame is None:
            first_valid_frame = total_frames
        last_valid_frame = total_frames

        if session.total_chunks:
            print(
                f"Recovered {len(session.chunks)}/{session.total_chunks} unique chunks "
                f"from {decoded_frames} decoded frames",
            )
        if session.is_complete:
            break

    if not session.is_complete:
        missing = session.missing_chunks()
        print(f"Frames scanned: {total_frames}")
        print(f"Decoded frames: {decoded_frames}")
        print(f"Duplicate chunks: {session.duplicate_chunks}")
        print(f"No-quad frames: {no_quad_failures}")
        print(f"Frame CRC failures: {frame_crc_failures}")
        print(f"Packet failures: {packet_failures}")
        if debug_dir is not None:
            print(f"Debug samples written: {debug_written}")
            print(f"Debug directory: {debug_dir}")
        if first_valid_frame is None:
            raise RuntimeError("No valid data frames detected")
        print(f"Detected active segment: frames {first_valid_frame}-{last_valid_frame}")
        raise RuntimeError(f"Missing chunks: {format_missing_ranges(missing)}")

    assembled = session.assemble_bytes()
    decoded = decode_transport_payload(assembled)
    output_file.write_bytes(decoded.original_bytes)
    print(f"Frames scanned: {total_frames}")
    print(f"Decoded frames: {decoded_frames}")
    print(f"Duplicate chunks: {session.duplicate_chunks}")
    print(f"No-quad frames: {no_quad_failures}")
    print(f"Frame CRC failures: {frame_crc_failures}")
    print(f"Packet failures: {packet_failures}")
    if debug_dir is not None:
        print(f"Debug samples written: {debug_written}")
        print(f"Debug directory: {debug_dir}")
    if first_valid_frame is not None and last_valid_frame is not None:
        print(f"Detected active segment: frames {first_valid_frame}-{last_valid_frame}")
    print(f"Compression: {decoded.compression}")
    print(f"Recovered bytes: {len(decoded.original_bytes)}")
    print(f"Wrote recovered file to {output_file}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Encode files into screen-recordable videos and recover them.")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    subparsers = parser.add_subparsers(dest="command", required=True)

    encode_parser = subparsers.add_parser("encode", help="Encode a file into a video")
    encode_parser.add_argument("input_file", type=Path)
    encode_parser.add_argument("output_video", type=Path)
    encode_parser.add_argument("--chunk-size", type=int, default=DEFAULT_CHUNK_SIZE)
    encode_parser.add_argument("--repeat", type=int, default=3)
    encode_parser.add_argument("--fps", type=int, default=8)
    encode_parser.add_argument("--compression", choices=("none", "gzip", "zstd"), default="zstd")
    encode_parser.add_argument("-y", "--yes", action="store_true", help="Skip the confirmation prompt")

    decode_parser = subparsers.add_parser("decode", help="Decode a recording back into a file")
    decode_parser.add_argument("input_video", type=Path)
    decode_parser.add_argument("output_file", type=Path)
    decode_parser.add_argument("--debug-dir", type=Path, help="Write sample failure diagnostics to this directory")
    decode_parser.add_argument("--debug-limit", type=int, default=12, help="Maximum number of failed frames to export")

    estimate_parser = subparsers.add_parser("estimate", help="Estimate output size and duration for each compression mode")
    estimate_parser.add_argument("input_file", type=Path)
    estimate_parser.add_argument("--chunk-size", type=int, default=DEFAULT_CHUNK_SIZE)
    estimate_parser.add_argument("--repeat", type=int, default=3)
    estimate_parser.add_argument("--fps", type=int, default=8)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "encode":
        encode_file_to_video(
            args.input_file,
            args.output_video,
            chunk_size=args.chunk_size,
            repeat=args.repeat,
            fps=args.fps,
            compression=args.compression,
            skip_confirmation=args.yes,
        )
    elif args.command == "decode":
        decode_video_to_file(
            args.input_video,
            args.output_file,
            debug_dir=args.debug_dir,
            debug_limit=args.debug_limit,
        )
    elif args.command == "estimate":
        print_estimates(
            args.input_file,
            chunk_size=args.chunk_size,
            repeat=args.repeat,
            fps=args.fps,
            output_suffix=".mp4",
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
