from __future__ import annotations

import argparse
from pathlib import Path

from screenfile.chunking import DEFAULT_CHUNK_SIZE, build_packets_from_file
from screenfile.frame_codec import FRAME_HEIGHT, FRAME_WIDTH, decode_frame_with_status, encode_packet_frame
from screenfile.recovery import RecoverySession, format_missing_ranges
from screenfile.video_io import read_video_frames, write_video


def encode_file_to_video(
    input_file: Path,
    output_video: Path,
    *,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
    repeat: int = 3,
    fps: int = 8,
) -> None:
    packets = build_packets_from_file(input_file, chunk_size=chunk_size)

    def iter_frames():
        for _ in range(repeat):
            for packet in packets:
                yield encode_packet_frame(packet)

    write_video(output_video, iter_frames(), fps=fps, frame_size=(FRAME_WIDTH, FRAME_HEIGHT))

    estimated_seconds = (len(packets) * repeat) / fps
    print(f"Input bytes: {input_file.stat().st_size}")
    print(f"Chunks: {len(packets)}")
    print(f"Repeat count: {repeat}")
    print(f"Estimated duration: {estimated_seconds:.1f}s")
    print("Playback tips: fullscreen, max brightness, avoid window animations.")


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

    session.write_file(output_file)
    print(f"Frames scanned: {total_frames}")
    print(f"Decoded frames: {decoded_frames}")
    print(f"Duplicate chunks: {session.duplicate_chunks}")
    print(f"No-quad frames: {no_quad_failures}")
    print(f"Frame CRC failures: {frame_crc_failures}")
    print(f"Packet failures: {packet_failures}")
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

    decode_parser = subparsers.add_parser("decode", help="Decode a recording back into a file")
    decode_parser.add_argument("input_video", type=Path)
    decode_parser.add_argument("output_file", type=Path)
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
        )
    elif args.command == "decode":
        decode_video_to_file(args.input_video, args.output_file)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
