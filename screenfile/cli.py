from __future__ import annotations

import argparse
from pathlib import Path

from screenfile.chunking import DEFAULT_CHUNK_SIZE, build_packets_from_bytes
from screenfile.frame_codec import FRAME_HEIGHT, FRAME_WIDTH, decode_frame_with_status, encode_packet_frame
from screenfile.payload_codec import decode_transport_payload, encode_transport_payload
from screenfile.recovery import RecoverySession, format_missing_ranges
from screenfile.video_io import read_video_frames, write_video


def _estimate_for_compression(
    input_file: Path,
    *,
    chunk_size: int,
    repeat: int,
    fps: int,
    compression: str,
) -> dict[str, float | int | str]:
    original_bytes = input_file.read_bytes()
    transport_payload = encode_transport_payload(input_file.name, original_bytes, compression=compression)
    packets = build_packets_from_bytes(input_file.name, transport_payload, chunk_size=chunk_size)
    frames = len(packets) * repeat
    return {
        "compression": compression,
        "original_bytes": len(original_bytes),
        "encoded_bytes": len(transport_payload),
        "ratio": len(transport_payload) / max(1, len(original_bytes)),
        "chunks": len(packets),
        "frames": frames,
        "duration": frames / fps,
    }


def _print_estimate(summary: dict[str, float | int | str]) -> None:
    print(f"Compression: {summary['compression']}")
    print(f"Original bytes: {summary['original_bytes']}")
    print(f"Encoded bytes: {summary['encoded_bytes']}")
    print(f"Compression ratio: {summary['ratio']:.3f}")
    print(f"Chunks: {summary['chunks']}")
    print(f"Frames: {summary['frames']}")
    print(f"Estimated duration: {summary['duration']:.1f}s")


def print_estimates(
    input_file: Path,
    *,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
    repeat: int = 3,
    fps: int = 8,
) -> None:
    for compression in ("none", "gzip", "zstd"):
        _print_estimate(
            _estimate_for_compression(
                input_file,
                chunk_size=chunk_size,
                repeat=repeat,
                fps=fps,
                compression=compression,
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


def decode_video_to_file(input_video: Path, output_file: Path) -> None:
    session = RecoverySession()
    total_frames = 0
    decoded_frames = 0
    frame_crc_failures = 0
    packet_failures = 0
    no_quad_failures = 0

    for frame in read_video_frames(input_video):
        total_frames += 1
        packet, status = decode_frame_with_status(frame)
        if status != "ok":
            if status == "frame-crc":
                frame_crc_failures += 1
            elif status == "packet-invalid":
                packet_failures += 1
            else:
                no_quad_failures += 1
            continue

        assert packet is not None
        try:
            session.add_packet(packet)
        except ValueError:
            packet_failures += 1
            continue
        decoded_frames += 1

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
    print(f"Compression: {decoded.compression}")
    print(f"Recovered bytes: {len(decoded.original_bytes)}")
    print(f"Wrote recovered file to {output_file}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Encode files into screen-recordable videos and recover them.")
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
        decode_video_to_file(args.input_video, args.output_file)
    elif args.command == "estimate":
        print_estimates(
            args.input_file,
            chunk_size=args.chunk_size,
            repeat=args.repeat,
            fps=args.fps,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
