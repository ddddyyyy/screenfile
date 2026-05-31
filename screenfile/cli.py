from __future__ import annotations

import argparse
import cv2
import numpy as np
import os
import tempfile
from concurrent.futures import ThreadPoolExecutor
from itertools import count
from pathlib import Path

from screenfile import __version__
from screenfile.chunking import DEFAULT_CHUNK_SIZE, HEADER_SIZE, build_packets_from_bytes, packet_from_bytes
from screenfile.frame_codec import (
    COLOR_LAYOUT,
    FRAME_HEIGHT,
    FRAME_WIDTH,
    MAX_PACKET_BYTES,
    _bits_to_packet,
    decode_color_frame_fast,
    encode_packet_frame,
    inspect_frame,
    vote_bit_candidates_from_frame,
)
from screenfile.payload_codec import decode_transport_payload, encode_transport_payload
from screenfile.recovery import RecoverySession, format_missing_ranges
from screenfile.video_io import read_video_frames, write_video

MAX_CHUNK_SIZE = MAX_PACKET_BYTES - HEADER_SIZE


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


def _validate_chunk_size(chunk_size: int) -> None:
    if chunk_size <= 0:
        raise ValueError("chunk_size must be positive")
    if chunk_size > MAX_CHUNK_SIZE:
        raise ValueError(
            "chunk_size is too large for the current frame format: "
            f"{chunk_size} > {MAX_CHUNK_SIZE}. "
            f"Each frame can carry at most {MAX_PACKET_BYTES} total packet bytes, "
            f"and {HEADER_SIZE} bytes are reserved for packet metadata, "
            f"so the payload chunk_size limit is {MAX_CHUNK_SIZE}. "
            f"Try --chunk-size {MAX_CHUNK_SIZE} or lower.",
        )


def _build_estimate_sample_frames(
    packets,
    *,
    repeat: int,
    mode: str = "matrix",
    max_sample_frames: int = 24,
) -> list:
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
        frames.append(encode_packet_frame(packet, mode=mode))
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
    mode: str = "matrix",
    output_suffix: str = ".mp4",
) -> dict[str, float | int | str]:
    _validate_chunk_size(chunk_size)
    original_bytes = input_file.read_bytes()
    transport_payload = encode_transport_payload(input_file.name, original_bytes, compression=compression)
    packets = build_packets_from_bytes(input_file.name, transport_payload, chunk_size=chunk_size)
    frames = len(packets) * repeat
    sample_frames = _build_estimate_sample_frames(packets, repeat=repeat, mode=mode)
    estimated_video_size = _estimate_video_size_bytes(
        sample_frames=sample_frames,
        total_frames=frames,
        fps=fps,
        suffix=output_suffix,
    )
    return {
        "compression": compression,
        "mode": mode,
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
    print(f"Mode: {summary['mode']}")
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
    mode: str = "matrix",
    output_suffix: str = ".mp4",
) -> None:
    _validate_chunk_size(chunk_size)
    for compression in ("none", "gzip", "zstd"):
        _print_estimate(
            _estimate_for_compression(
                input_file,
                chunk_size=chunk_size,
                repeat=repeat,
                fps=fps,
                compression=compression,
                mode=mode,
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
    mode: str = "matrix",
    skip_confirmation: bool = False,
) -> None:
    _validate_chunk_size(chunk_size)
    summary = _estimate_for_compression(
        input_file,
        chunk_size=chunk_size,
        repeat=repeat,
        fps=fps,
        compression=compression,
        mode=mode,
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
        for packet in packets:
            for _ in range(repeat):
                yield encode_packet_frame(packet, mode=mode)

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


def _bit_distance(left: np.ndarray, right: np.ndarray) -> int:
    return int(np.count_nonzero(left != right))


def _cluster_vote_bits(candidates, *, max_distance_ratio: float = 0.16) -> list[dict[str, object]]:
    clusters: list[dict[str, object]] = []

    for layout, candidate_bits in candidates:
        bits = candidate_bits.astype(np.uint8, copy=False)
        limit = int(bits.size * max_distance_ratio)
        best_cluster: dict[str, object] | None = None
        best_distance = limit + 1

        for cluster in clusters:
            if cluster["layout"] != layout:
                continue
            reference = cluster["reference"]
            distance = _bit_distance(bits, reference)
            if distance < best_distance:
                best_distance = distance
                best_cluster = cluster

        if best_cluster is None or best_distance > limit:
            clusters.append(
                {
                    "layout": layout,
                    "reference": bits.copy(),
                    "samples": [bits.copy()],
                }
            )
            continue
        best_cluster["samples"].append(bits.copy())
    return clusters


def _packets_from_vote_clusters(clusters: list[dict[str, object]]) -> list:
    recovered = []

    for cluster in clusters:
        samples = cluster["samples"]
        if len(samples) < 2:
            continue
        votes = np.stack(samples, axis=0)
        voted_bits = (votes.sum(axis=0) * 2 >= votes.shape[0]).astype(np.uint8)
        try:
            packet_bytes = _bits_to_packet(voted_bits, cluster["layout"])
        except ValueError:
            continue
        recovered.append(packet_from_bytes(packet_bytes))
    return recovered


def _packet_from_voted_bits(samples: list[np.ndarray], layout) -> object | None:
    votes = np.stack(samples, axis=0)
    voted_bits = (votes.sum(axis=0) * 2 >= votes.shape[0]).astype(np.uint8)
    try:
        packet_bytes = _bits_to_packet(voted_bits, layout)
    except ValueError:
        return None
    return packet_from_bytes(packet_bytes)


def _recover_packets_by_bit_voting(inspections: list, *, max_distance_ratio: float = 0.16) -> list:
    candidates = (
        (inspection.layout, inspection.bits)
        for inspection in inspections
        if (
            (inspection.status == "frame-crc" or inspection.layout == COLOR_LAYOUT)
            and inspection.bits is not None
            and inspection.layout is not None
        )
    )
    return _packets_from_vote_clusters(_cluster_vote_bits(candidates, max_distance_ratio=max_distance_ratio))


def _recover_packets_by_temporal_bit_voting(
    timed_inspections: list[tuple[int, object]],
    *,
    window_size: int = 10,
    min_samples: int = 3,
) -> list:
    candidates = [
        (frame_number, inspection.layout, inspection.bits)
        for frame_number, inspection in sorted(timed_inspections, key=lambda item: item[0])
        if (
            (inspection.status == "frame-crc" or inspection.layout == COLOR_LAYOUT)
            and inspection.bits is not None
            and inspection.layout is not None
        )
    ]
    recovered = {}

    for start_index, (_frame_number, layout, _bits) in enumerate(candidates):
        samples: list[np.ndarray] = []
        for _candidate_frame, candidate_layout, candidate_bits in candidates[start_index : start_index + window_size]:
            if candidate_layout != layout:
                continue
            samples.append(candidate_bits.astype(np.uint8, copy=False))
            if len(samples) < min_samples:
                continue
            packet = _packet_from_voted_bits(samples, layout)
            if packet is not None:
                recovered[(packet.file_id, packet.chunk_index)] = packet

    return list(recovered.values())


def _recover_packets_by_salvage_scan(input_video: Path, *, frame_step: int = 2) -> list:
    candidates = []
    for frame_number, frame in enumerate(read_video_frames(input_video), start=1):
        if frame_step > 1 and (frame_number - 1) % frame_step != 0:
            continue
        candidates.extend(vote_bit_candidates_from_frame(frame))
    return _packets_from_vote_clusters(_cluster_vote_bits(candidates))


def _predicted_frame_numbers(
    *,
    total_frames: int,
    total_chunks: int,
    offsets: tuple[int, ...] = (0, -2, 2, -4, 4, -8, 8),
) -> list[int]:
    if total_frames <= 0 or total_chunks <= 0:
        return []

    frame_numbers: set[int] = set()
    for chunk_index in range(total_chunks):
        base = round(chunk_index * (total_frames - 1) / total_chunks)
        for offset in offsets:
            frame_numbers.add(max(0, min(total_frames - 1, base + offset)))
    frame_numbers.add(total_frames - 1)
    return sorted(frame_numbers)


def _try_fast_color_decode(
    input_video: Path,
    output_file: Path,
    *,
    workers: int,
) -> bool:
    cap = cv2.VideoCapture(str(input_video))
    if not cap.isOpened():
        return False
    total_video_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    cap.release()
    if total_video_frames <= 0:
        return False

    session = RecoverySession()
    decoded_frames = 0
    sampled_frames = 0
    first_valid_frame: int | None = None
    last_valid_frame: int | None = None
    next_progress_report = 25
    attempted_frame_indexes: set[int] = set()

    def add_packet(packet, frame_number: int) -> None:
        nonlocal decoded_frames
        nonlocal first_valid_frame
        nonlocal last_valid_frame
        nonlocal next_progress_report

        before = len(session.chunks)
        session.add_packet(packet)
        decoded_frames += 1
        if first_valid_frame is None:
            first_valid_frame = frame_number
        last_valid_frame = frame_number
        if len(session.chunks) > before and session.total_chunks:
            should_report = len(session.chunks) >= next_progress_report or session.is_complete
            if should_report:
                print(
                    f"Fast color recovered {len(session.chunks)}/{session.total_chunks} unique chunks "
                    f"from {decoded_frames} decoded frames",
                )
                while next_progress_report <= len(session.chunks):
                    next_progress_report += 25

    scout_limit = min(total_video_frames, 12)
    scout_frames = read_video_frames(input_video)
    for frame_index in range(scout_limit):
        try:
            frame = next(scout_frames)
        except StopIteration:
            break
        attempted_frame_indexes.add(frame_index)
        sampled_frames += 1
        packet = decode_color_frame_fast(frame)
        if packet is None:
            continue
        try:
            add_packet(packet, frame_index + 1)
        except ValueError:
            return False
        break

    if not session.total_chunks:
        return False

    target_indexes = _predicted_frame_numbers(total_frames=total_video_frames, total_chunks=session.total_chunks)
    target_set = set(target_indexes)
    target_set.difference_update(attempted_frame_indexes)
    batch_size = max(1, workers * 4)

    with ThreadPoolExecutor(max_workers=workers) as executor:
        batch: list[tuple[int, object]] = []
        for frame_index, frame in enumerate(read_video_frames(input_video)):
            if frame_index not in target_set:
                continue
            batch.append((frame_index, frame))
            if len(batch) < batch_size:
                continue

            packets = executor.map(decode_color_frame_fast, (item[1] for item in batch))
            for (batched_frame_index, _batched_frame), packet in zip(batch, packets):
                sampled_frames += 1
                if packet is None:
                    continue
                try:
                    add_packet(packet, batched_frame_index + 1)
                except ValueError:
                    continue
                if session.is_complete:
                    break
            batch.clear()
            if session.is_complete:
                break

        if batch and not session.is_complete:
            packets = executor.map(decode_color_frame_fast, (item[1] for item in batch))
            for (batched_frame_index, _batched_frame), packet in zip(batch, packets):
                sampled_frames += 1
                if packet is None:
                    continue
                try:
                    add_packet(packet, batched_frame_index + 1)
                except ValueError:
                    continue
                if session.is_complete:
                    break

    if not session.is_complete:
        print(
            f"Fast color decode found {len(session.chunks)}/{session.total_chunks} chunks; "
            "falling back to full scan.",
        )
        return False

    assembled = session.assemble_bytes()
    decoded = decode_transport_payload(assembled)
    output_file.write_bytes(decoded.original_bytes)
    print(f"Frames scanned: {total_video_frames}")
    print(f"Fast color frames sampled: {sampled_frames}")
    print(f"Decoded frames: {decoded_frames}")
    print(f"Duplicate chunks: {session.duplicate_chunks}")
    if first_valid_frame is not None and last_valid_frame is not None:
        print(f"Detected active segment: frames {first_valid_frame}-{last_valid_frame}")
    print(f"Compression: {decoded.compression}")
    print(f"Recovered bytes: {len(decoded.original_bytes)}")
    print(f"Wrote recovered file to {output_file}")
    return True


def decode_video_to_file(
    input_video: Path,
    output_file: Path,
    *,
    debug_dir: Path | None = None,
    debug_limit: int = 12,
    workers: int | None = None,
) -> None:
    session = RecoverySession()
    total_frames = 0
    decoded_frames = 0
    frame_crc_failures = 0
    packet_failures = 0
    no_quad_failures = 0
    debug_written = 0
    vote_candidate_inspections = []
    temporal_vote_candidate_inspections = []
    vote_recovered_chunks = 0
    first_valid_frame: int | None = None
    last_valid_frame: int | None = None
    decode_workers = workers if workers is not None else max(1, min(8, (os.cpu_count() or 2)))
    if decode_workers <= 0:
        raise ValueError("workers must be positive")

    if _try_fast_color_decode(input_video, output_file, workers=decode_workers):
        return

    batch_size = max(1, decode_workers * 4)
    next_progress_report = 25

    def process_inspection(frame_number: int, frame, inspection) -> bool:
        nonlocal total_frames
        nonlocal decoded_frames
        nonlocal frame_crc_failures
        nonlocal packet_failures
        nonlocal no_quad_failures
        nonlocal debug_written
        nonlocal vote_candidate_inspections
        nonlocal temporal_vote_candidate_inspections
        nonlocal first_valid_frame
        nonlocal last_valid_frame
        nonlocal next_progress_report

        total_frames = frame_number
        packet, status = inspection.packet, inspection.status

        if status != "ok":
            if status == "frame-crc":
                frame_crc_failures += 1
                if inspection.bits is not None and inspection.layout is not None:
                    vote_candidate_inspections.append(inspection)
                    temporal_vote_candidate_inspections.append((frame_number, inspection))
            elif status == "packet-invalid":
                packet_failures += 1
                if inspection.bits is not None and inspection.layout is not None:
                    temporal_vote_candidate_inspections.append((frame_number, inspection))
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
            return False

        assert packet is not None
        try:
            session.add_packet(packet)
        except ValueError:
            packet_failures += 1
            return False
        decoded_frames += 1
        if first_valid_frame is None:
            first_valid_frame = frame_number
        last_valid_frame = frame_number

        if session.total_chunks:
            should_report = len(session.chunks) >= next_progress_report or session.is_complete
            if should_report:
                print(
                    f"Recovered {len(session.chunks)}/{session.total_chunks} unique chunks "
                    f"from {decoded_frames} decoded frames",
                )
                while next_progress_report <= len(session.chunks):
                    next_progress_report += 25
        return session.is_complete

    frame_numbers = count(1)
    with ThreadPoolExecutor(max_workers=decode_workers) as executor:
        batch: list[tuple[int, object]] = []
        for frame in read_video_frames(input_video):
            frame_number = next(frame_numbers)
            batch.append((frame_number, frame))
            if len(batch) < batch_size:
                continue

            inspections = executor.map(inspect_frame, (item[1] for item in batch))
            for (batched_frame_number, batched_frame), inspection in zip(batch, inspections):
                if process_inspection(batched_frame_number, batched_frame, inspection):
                    break
            batch.clear()
            if session.is_complete:
                break

        if batch and not session.is_complete:
            inspections = executor.map(inspect_frame, (item[1] for item in batch))
            for (batched_frame_number, batched_frame), inspection in zip(batch, inspections):
                if process_inspection(batched_frame_number, batched_frame, inspection):
                    break

    if not session.is_complete and vote_candidate_inspections:
        for packet in _recover_packets_by_bit_voting(vote_candidate_inspections):
            before = len(session.chunks)
            try:
                session.add_packet(packet)
            except ValueError:
                packet_failures += 1
                continue
            if len(session.chunks) > before:
                vote_recovered_chunks += 1
        if vote_recovered_chunks:
            print(f"Recovered {vote_recovered_chunks} additional chunks by bit voting")

    if not session.is_complete and temporal_vote_candidate_inspections:
        temporal_recovered_chunks = 0
        for packet in _recover_packets_by_temporal_bit_voting(temporal_vote_candidate_inspections):
            before = len(session.chunks)
            try:
                session.add_packet(packet)
            except ValueError:
                packet_failures += 1
                continue
            if len(session.chunks) > before:
                temporal_recovered_chunks += 1
        if temporal_recovered_chunks:
            vote_recovered_chunks += temporal_recovered_chunks
            print(f"Recovered {temporal_recovered_chunks} additional chunks by temporal bit voting")

    if not session.is_complete:
        salvage_recovered_chunks = 0
        for packet in _recover_packets_by_salvage_scan(input_video):
            before = len(session.chunks)
            try:
                session.add_packet(packet)
            except ValueError:
                packet_failures += 1
                continue
            if len(session.chunks) > before:
                salvage_recovered_chunks += 1
        if salvage_recovered_chunks:
            vote_recovered_chunks += salvage_recovered_chunks
            print(f"Recovered {salvage_recovered_chunks} additional chunks by salvage bit voting")

    if not session.is_complete:
        missing = session.missing_chunks()
        print(f"Frames scanned: {total_frames}")
        print(f"Decoded frames: {decoded_frames}")
        print(f"Bit-vote recovered chunks: {vote_recovered_chunks}")
        print(f"Duplicate chunks: {session.duplicate_chunks}")
        print(f"No-quad frames: {no_quad_failures}")
        print(f"Frame CRC failures: {frame_crc_failures}")
        print(f"Packet failures: {packet_failures}")
        if debug_dir is not None:
            print(f"Debug samples written: {debug_written}")
            print(f"Debug directory: {debug_dir}")
        if first_valid_frame is None and vote_recovered_chunks == 0:
            raise RuntimeError("No valid data frames detected")
        if first_valid_frame is not None:
            print(f"Detected active segment: frames {first_valid_frame}-{last_valid_frame}")
        raise RuntimeError(f"Missing chunks: {format_missing_ranges(missing)}")

    assembled = session.assemble_bytes()
    decoded = decode_transport_payload(assembled)
    output_file.write_bytes(decoded.original_bytes)
    print(f"Frames scanned: {total_frames}")
    print(f"Decoded frames: {decoded_frames}")
    print(f"Bit-vote recovered chunks: {vote_recovered_chunks}")
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
    encode_parser.add_argument(
        "--chunk-size",
        type=int,
        default=DEFAULT_CHUNK_SIZE,
        help=f"Payload bytes per chunk (default: {DEFAULT_CHUNK_SIZE}, max: {MAX_CHUNK_SIZE})",
    )
    encode_parser.add_argument("--repeat", type=int, default=3)
    encode_parser.add_argument("--fps", type=int, default=8)
    encode_parser.add_argument("--compression", choices=("none", "gzip", "zstd"), default="zstd")
    encode_parser.add_argument("--mode", choices=("matrix", "color"), default="matrix")
    encode_parser.add_argument("-y", "--yes", action="store_true", help="Skip the confirmation prompt")

    decode_parser = subparsers.add_parser("decode", help="Decode a recording back into a file")
    decode_parser.add_argument("input_video", type=Path)
    decode_parser.add_argument("output_file", type=Path)
    decode_parser.add_argument("--debug-dir", type=Path, help="Write sample failure diagnostics to this directory")
    decode_parser.add_argument("--debug-limit", type=int, default=12, help="Maximum number of failed frames to export")
    decode_parser.add_argument(
        "--workers",
        type=int,
        default=None,
        help="Parallel frame decode workers (default: min(8, CPU count))",
    )

    estimate_parser = subparsers.add_parser("estimate", help="Estimate output size and duration for each compression mode")
    estimate_parser.add_argument("input_file", type=Path)
    estimate_parser.add_argument(
        "--chunk-size",
        type=int,
        default=DEFAULT_CHUNK_SIZE,
        help=f"Payload bytes per chunk (default: {DEFAULT_CHUNK_SIZE}, max: {MAX_CHUNK_SIZE})",
    )
    estimate_parser.add_argument("--repeat", type=int, default=3)
    estimate_parser.add_argument("--fps", type=int, default=8)
    estimate_parser.add_argument("--mode", choices=("matrix", "color"), default="matrix")
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
            mode=args.mode,
            skip_confirmation=args.yes,
        )
    elif args.command == "decode":
        decode_video_to_file(
            args.input_video,
            args.output_file,
            debug_dir=args.debug_dir,
            debug_limit=args.debug_limit,
            workers=args.workers,
        )
    elif args.command == "estimate":
        print_estimates(
            args.input_file,
            chunk_size=args.chunk_size,
            repeat=args.repeat,
            fps=args.fps,
            mode=args.mode,
            output_suffix=".mp4",
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
