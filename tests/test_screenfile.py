from __future__ import annotations

import io
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch


class ChunkingTests(unittest.TestCase):
    def test_packet_round_trip_preserves_metadata_and_payload(self) -> None:
        from screenfile.chunking import build_packets_from_bytes, packet_from_bytes

        packets = build_packets_from_bytes(
            "notes.txt",
            b"hello world" * 50,
            chunk_size=64,
        )

        restored = packet_from_bytes(packets[0].to_bytes())
        self.assertEqual(restored.chunk_index, 0)
        self.assertEqual(restored.total_chunks, len(packets))
        self.assertEqual(restored.payload, packets[0].payload)
        self.assertEqual(restored.file_sha256, packets[0].file_sha256)

    def test_recovery_reassembles_original_bytes(self) -> None:
        from screenfile.chunking import build_packets_from_bytes
        from screenfile.recovery import RecoverySession

        payload = b"abc123" * 300
        packets = build_packets_from_bytes("archive.bin", payload, chunk_size=128)
        session = RecoverySession()

        for packet in packets:
            session.add_packet(packet)

        self.assertTrue(session.is_complete)
        self.assertEqual(session.assemble_bytes(), payload)


class PayloadCodecTests(unittest.TestCase):
    def test_transport_payload_round_trip_with_zstd(self) -> None:
        from screenfile.payload_codec import decode_transport_payload, encode_transport_payload

        original = (b"hello payload " * 1000)[:12000]
        encoded = encode_transport_payload("sample.bin", original, compression="zstd")
        decoded = decode_transport_payload(encoded)

        self.assertEqual(decoded.compression, "zstd")
        self.assertEqual(decoded.original_name, "sample.bin")
        self.assertEqual(decoded.original_bytes, original)
        self.assertLess(decoded.encoded_size, len(original))

    def test_transport_payload_round_trip_with_gzip(self) -> None:
        from screenfile.payload_codec import decode_transport_payload, encode_transport_payload

        original = (b"gzip payload " * 1000)[:12000]
        encoded = encode_transport_payload("sample.bin", original, compression="gzip")
        decoded = decode_transport_payload(encoded)

        self.assertEqual(decoded.compression, "gzip")
        self.assertEqual(decoded.original_bytes, original)

    def test_legacy_payload_without_wrapper_passes_through(self) -> None:
        from screenfile.payload_codec import decode_transport_payload

        original = b"legacy-binary"
        decoded = decode_transport_payload(original)

        self.assertEqual(decoded.compression, "none")
        self.assertEqual(decoded.original_bytes, original)


class FrameCodecTests(unittest.TestCase):
    def test_frame_encode_decode_round_trip(self) -> None:
        from screenfile.chunking import build_packets_from_bytes
        from screenfile.frame_codec import decode_frame, encode_packet_frame

        packet = build_packets_from_bytes("frame.bin", b"x" * 300, chunk_size=300)[0]
        frame = encode_packet_frame(packet)
        restored = decode_frame(frame)

        self.assertIsNotNone(restored)
        self.assertEqual(restored.to_bytes(), packet.to_bytes())

    def test_frame_labels_stay_in_top_margin(self) -> None:
        from screenfile.chunking import build_packets_from_bytes
        from screenfile.frame_codec import DEFAULT_LAYOUT, FRAME_HEIGHT, FRAME_WIDTH, PROTOCOL_LABEL, encode_packet_frame

        packet = build_packets_from_bytes("frame.bin", b"x" * 300, chunk_size=300)[0]
        frame = encode_packet_frame(packet)

        code_start_x = (FRAME_WIDTH - DEFAULT_LAYOUT.code_width) // 2
        code_start_y = (FRAME_HEIGHT - DEFAULT_LAYOUT.code_height) // 2
        top_margin = frame[:code_start_y, :, :]
        left_label_region = top_margin[:, : code_start_x - 12, :]
        right_label_region = top_margin[:, code_start_x + DEFAULT_LAYOUT.code_width + 12 :, :]
        center_top_region = top_margin[:, code_start_x + 20 : code_start_x + DEFAULT_LAYOUT.code_width - 20, :]

        self.assertLess(left_label_region.mean(), 250)
        self.assertLess(right_label_region.mean(), 250)
        self.assertGreater(center_top_region.mean(), 240)
        self.assertEqual(PROTOCOL_LABEL, "layout=v3")

    def test_default_layout_uses_wide_screen_capacity(self) -> None:
        from screenfile.frame_codec import DEFAULT_LAYOUT, LAYOUT_V1, LAYOUT_V2, MAX_PACKET_BYTES

        self.assertEqual(DEFAULT_LAYOUT.label, "layout=v3")
        self.assertGreater(DEFAULT_LAYOUT.code_width / 1920, 0.85)
        self.assertGreater(DEFAULT_LAYOUT.cell_size, LAYOUT_V2.cell_size)
        self.assertGreater(MAX_PACKET_BYTES, LAYOUT_V1.max_packet_bytes)
        self.assertLess(DEFAULT_LAYOUT.grid_cols * DEFAULT_LAYOUT.grid_rows, LAYOUT_V2.grid_cols * LAYOUT_V2.grid_rows)

    def test_decoder_keeps_legacy_v1_layout_compatibility(self) -> None:
        from screenfile.chunking import build_packets_from_bytes
        from screenfile.frame_codec import LAYOUT_V1, decode_frame, encode_packet_frame

        packet = build_packets_from_bytes("legacy-frame.bin", b"legacy" * 80, chunk_size=360)[0]
        frame = encode_packet_frame(packet, layout=LAYOUT_V1)
        restored = decode_frame(frame)

        self.assertIsNotNone(restored)
        self.assertEqual(restored.to_bytes(), packet.to_bytes())

    def test_decoder_keeps_layout_v2_compatibility(self) -> None:
        from screenfile.chunking import build_packets_from_bytes
        from screenfile.frame_codec import LAYOUT_V2, decode_frame, encode_packet_frame

        packet = build_packets_from_bytes("layout-v2-frame.bin", b"layout-v2" * 80, chunk_size=640)[0]
        frame = encode_packet_frame(packet, layout=LAYOUT_V2)
        restored = decode_frame(frame)

        self.assertIsNotNone(restored)
        self.assertEqual(restored.to_bytes(), packet.to_bytes())

    def test_color_frame_encode_decode_round_trip(self) -> None:
        from screenfile.chunking import build_packets_from_bytes
        from screenfile.frame_codec import decode_frame, encode_packet_frame

        packet = build_packets_from_bytes("color-frame.bin", b"color-payload" * 60, chunk_size=768)[0]
        frame = encode_packet_frame(packet, mode="color")
        restored = decode_frame(frame)

        self.assertIsNotNone(restored)
        self.assertEqual(restored.to_bytes(), packet.to_bytes())

    def test_color_square_decode_tolerates_small_grid_offset(self) -> None:
        import cv2
        import numpy as np

        from screenfile.chunking import build_packets_from_bytes
        from screenfile.frame_codec import COLOR_LAYOUT, FRAME_HEIGHT, FRAME_WIDTH, inspect_color_square, encode_packet_frame

        packet = build_packets_from_bytes("color-offset.bin", b"color-offset-payload" * 50, chunk_size=768)[0]
        frame = encode_packet_frame(packet, mode="color")
        code_x = (FRAME_WIDTH - COLOR_LAYOUT.code_width) // 2
        code_y = (FRAME_HEIGHT - COLOR_LAYOUT.code_height) // 2
        code = frame[code_y : code_y + COLOR_LAYOUT.code_height, code_x : code_x + COLOR_LAYOUT.code_width]
        shifted = cv2.warpAffine(
            code,
            np.float32([[1, 0, 8], [0, 1, 6]]),
            (COLOR_LAYOUT.code_width, COLOR_LAYOUT.code_height),
            borderValue=(255, 255, 255),
        )

        restored = inspect_color_square(shifted, COLOR_LAYOUT).packet

        self.assertIsNotNone(restored)
        self.assertEqual(restored.to_bytes(), packet.to_bytes())

    def test_fast_color_candidate_decoder_recovers_color_frame(self) -> None:
        from screenfile.chunking import build_packets_from_bytes
        from screenfile.frame_codec import decode_color_frame_fast, encode_packet_frame

        packet = build_packets_from_bytes("fast-color.bin", b"fast-color-payload" * 50, chunk_size=768)[0]
        frame = encode_packet_frame(packet, mode="color")

        restored = decode_color_frame_fast(frame)

        self.assertIsNotNone(restored)
        self.assertEqual(restored.to_bytes(), packet.to_bytes())

    def test_color_layout_uses_larger_cells_without_losing_default_capacity(self) -> None:
        from screenfile.frame_codec import COLOR_LAYOUT, DEFAULT_LAYOUT

        self.assertEqual(COLOR_LAYOUT.label, "color-v1")
        self.assertGreater(COLOR_LAYOUT.cell_size, DEFAULT_LAYOUT.cell_size)
        self.assertGreaterEqual(COLOR_LAYOUT.max_packet_bytes, DEFAULT_LAYOUT.max_packet_bytes)

    def test_decoder_handles_mild_camera_like_degradation(self) -> None:
        import cv2
        import numpy as np

        from screenfile.chunking import build_packets_from_bytes
        from screenfile.frame_codec import LAYOUT_V1, decode_frame, encode_packet_frame

        packet = build_packets_from_bytes("camera.bin", b"payload" * 80, chunk_size=400)[0]
        frame = encode_packet_frame(packet, layout=LAYOUT_V1)

        src = np.float32(
            [
                [0, 0],
                [frame.shape[1] - 1, 0],
                [frame.shape[1] - 1, frame.shape[0] - 1],
                [0, frame.shape[0] - 1],
            ]
        )
        dst = np.float32(
            [
                [30, 50],
                [frame.shape[1] - 40, 10],
                [frame.shape[1] - 15, frame.shape[0] - 20],
                [15, frame.shape[0] - 5],
            ]
        )
        warped = cv2.warpPerspective(
            frame,
            cv2.getPerspectiveTransform(src, dst),
            (frame.shape[1], frame.shape[0]),
            borderValue=(255, 255, 255),
        )
        blurred = cv2.GaussianBlur(warped, (5, 5), 0)
        degraded = cv2.convertScaleAbs(blurred, alpha=0.9, beta=12)

        restored = decode_frame(degraded)
        self.assertIsNotNone(restored)
        self.assertEqual(restored.to_bytes(), packet.to_bytes())

    def test_decoder_handles_stronger_phone_capture_artifacts(self) -> None:
        import cv2
        import numpy as np

        from screenfile.chunking import build_packets_from_bytes
        from screenfile.frame_codec import decode_frame, encode_packet_frame

        packet = build_packets_from_bytes("phone.bin", b"payload" * 90, chunk_size=480)[0]
        frame = encode_packet_frame(packet)

        scaled = cv2.resize(frame, (1520, 980), interpolation=cv2.INTER_AREA)
        canvas = np.full((1080, 1920, 3), 245, dtype=np.uint8)
        y0, x0 = 40, 140
        canvas[y0 : y0 + scaled.shape[0], x0 : x0 + scaled.shape[1]] = scaled

        src = np.float32(
            [
                [x0, y0],
                [x0 + scaled.shape[1] - 1, y0],
                [x0 + scaled.shape[1] - 1, y0 + scaled.shape[0] - 1],
                [x0, y0 + scaled.shape[0] - 1],
            ]
        )
        dst = np.float32(
            [
                [220, 120],
                [1650, 40],
                [1710, 1000],
                [150, 1040],
            ]
        )
        warped = cv2.warpPerspective(
            canvas,
            cv2.getPerspectiveTransform(src, dst),
            (1920, 1080),
            borderValue=(250, 250, 250),
        )
        blurred = cv2.GaussianBlur(warped, (9, 9), 0)
        low_contrast = cv2.convertScaleAbs(blurred, alpha=0.78, beta=32)

        noise = np.random.default_rng(7).normal(0, 10, low_contrast.shape).astype(np.int16)
        noisy = np.clip(low_contrast.astype(np.int16) + noise, 0, 255).astype(np.uint8)

        restored = decode_frame(noisy)
        self.assertIsNotNone(restored)
        self.assertEqual(restored.to_bytes(), packet.to_bytes())

    def test_decoder_handles_localized_glare(self) -> None:
        import cv2
        import numpy as np

        from screenfile.chunking import build_packets_from_bytes
        from screenfile.frame_codec import decode_frame, encode_packet_frame

        packet = build_packets_from_bytes("glare.bin", b"payload" * 90, chunk_size=480)[0]
        frame = encode_packet_frame(packet).astype(np.float32)

        glare = np.zeros(frame.shape[:2], dtype=np.float32)
        cv2.circle(glare, (620, 390), 105, 1.0, -1)
        glare = cv2.GaussianBlur(glare, (0, 0), sigmaX=42, sigmaY=42)
        glare = np.clip(glare * 120, 0, 120)

        degraded = frame.copy()
        degraded[:, :, 1] = np.clip(degraded[:, :, 1] + glare, 0, 255)
        degraded[:, :, 2] = np.clip(degraded[:, :, 2] + glare, 0, 255)
        degraded = degraded * (1.0 - glare[..., None] / 255.0 * 0.28) + 255.0 * (glare[..., None] / 255.0 * 0.28)
        degraded = np.clip(degraded, 0, 255).astype(np.uint8)

        restored = decode_frame(degraded)
        self.assertIsNotNone(restored)
        self.assertEqual(restored.to_bytes(), packet.to_bytes())

    def test_decoder_handles_rotated_embedded_frame(self) -> None:
        import cv2
        import numpy as np

        from screenfile.chunking import build_packets_from_bytes
        from screenfile.frame_codec import decode_frame, encode_packet_frame

        packet = build_packets_from_bytes("rotated.bin", b"rotated-payload" * 40, chunk_size=560)[0]
        frame = encode_packet_frame(packet)

        canvas = np.full((1400, 2200, 3), 245, dtype=np.uint8)
        scaled = cv2.resize(frame, (1400, 900), interpolation=cv2.INTER_AREA)
        y0, x0 = 250, 380
        canvas[y0 : y0 + scaled.shape[0], x0 : x0 + scaled.shape[1]] = scaled

        matrix = cv2.getRotationMatrix2D((1100, 700), 13, 1.0)
        rotated = cv2.warpAffine(canvas, matrix, (2200, 1400), borderValue=(250, 250, 250))

        restored = decode_frame(rotated)
        self.assertIsNotNone(restored)
        self.assertEqual(restored.to_bytes(), packet.to_bytes())

    def test_decoder_finds_non_fullscreen_data_region_among_larger_ui_blocks(self) -> None:
        import cv2
        import numpy as np

        from screenfile.chunking import build_packets_from_bytes
        from screenfile.frame_codec import decode_frame, encode_packet_frame

        packet = build_packets_from_bytes("windowed.bin", b"windowed-payload" * 50, chunk_size=560)[0]
        frame = encode_packet_frame(packet)

        canvas = np.full((1600, 2400, 3), 245, dtype=np.uint8)
        cv2.rectangle(canvas, (80, 80), (2200, 350), (225, 225, 225), -1)
        cv2.rectangle(canvas, (100, 420), (2280, 1450), (230, 230, 230), -1)
        cv2.rectangle(canvas, (1550, 500), (2320, 1500), (210, 210, 210), -1)

        scaled = cv2.resize(frame, (1700, 960), interpolation=cv2.INTER_AREA)
        y0, x0 = 470, 300
        canvas[y0 : y0 + scaled.shape[0], x0 : x0 + scaled.shape[1]] = scaled

        src = np.float32(
            [
                [x0, y0],
                [x0 + scaled.shape[1] - 1, y0],
                [x0 + scaled.shape[1] - 1, y0 + scaled.shape[0] - 1],
                [x0, y0 + scaled.shape[0] - 1],
            ]
        )
        dst = np.float32(
            [
                [360, 500],
                [1970, 455],
                [2030, 1410],
                [260, 1455],
            ]
        )
        warped = cv2.warpPerspective(
            canvas,
            cv2.getPerspectiveTransform(src, dst),
            (2400, 1600),
            borderValue=(245, 245, 245),
        )

        restored = decode_frame(warped)
        self.assertIsNotNone(restored)
        self.assertEqual(restored.to_bytes(), packet.to_bytes())

    def test_decoder_crops_code_region_from_visible_full_video_frame(self) -> None:
        import cv2
        import numpy as np

        from screenfile.chunking import build_packets_from_bytes
        from screenfile.frame_codec import DEFAULT_LAYOUT, FRAME_HEIGHT, FRAME_WIDTH, decode_frame, encode_packet_frame

        packet = build_packets_from_bytes("visible-video-frame.bin", b"visible-video-frame" * 70, chunk_size=768)[0]
        frame = encode_packet_frame(packet)

        code_x = (FRAME_WIDTH - DEFAULT_LAYOUT.code_width) // 2
        code_y = (FRAME_HEIGHT - DEFAULT_LAYOUT.code_height) // 2
        data_x = code_x + (DEFAULT_LAYOUT.code_width - DEFAULT_LAYOUT.data_width) // 2
        data_y = code_y + (DEFAULT_LAYOUT.code_height - DEFAULT_LAYOUT.data_height) // 2
        cv2.rectangle(
            frame,
            (data_x + DEFAULT_LAYOUT.data_width + 5, data_y + DEFAULT_LAYOUT.data_height - 40),
            (FRAME_WIDTH - 1, FRAME_HEIGHT - 1),
            (30, 30, 30),
            -1,
        )
        cv2.rectangle(frame, (0, 0), (FRAME_WIDTH - 1, FRAME_HEIGHT - 1), (0, 0, 0), 10)

        canvas = np.full((1400, 2400, 3), 220, dtype=np.uint8)
        scaled = cv2.resize(frame, (1900, 1069), interpolation=cv2.INTER_AREA)
        y0, x0 = 180, 250
        canvas[y0 : y0 + scaled.shape[0], x0 : x0 + scaled.shape[1]] = scaled

        src = np.float32(
            [
                [x0, y0],
                [x0 + scaled.shape[1] - 1, y0],
                [x0 + scaled.shape[1] - 1, y0 + scaled.shape[0] - 1],
                [x0, y0 + scaled.shape[0] - 1],
            ]
        )
        dst = np.float32(
            [
                [210, 230],
                [2180, 170],
                [2240, 1260],
                [150, 1320],
            ]
        )
        warped = cv2.warpPerspective(
            canvas,
            cv2.getPerspectiveTransform(src, dst),
            (2400, 1400),
            borderValue=(220, 220, 220),
        )

        restored = decode_frame(warped)
        self.assertIsNotNone(restored)
        self.assertEqual(restored.to_bytes(), packet.to_bytes())

    def test_bit_vote_recovery_repairs_different_bit_errors_across_frames(self) -> None:
        import numpy as np

        from screenfile.chunking import build_packets_from_bytes
        from screenfile.cli import _recover_packets_by_bit_voting
        from screenfile.frame_codec import DEFAULT_LAYOUT, FrameInspection, _bits_to_packet, _packet_to_bits

        packet = build_packets_from_bytes("vote.bin", b"vote-payload" * 50, chunk_size=640)[0]
        clean_bits = _packet_to_bits(packet.to_bytes(), DEFAULT_LAYOUT)
        rng = np.random.default_rng(11)
        inspections = []

        for _ in range(5):
            damaged = clean_bits.copy()
            damaged[rng.choice(clean_bits.size, size=500, replace=False)] ^= 1
            with self.assertRaises(ValueError):
                _bits_to_packet(damaged, DEFAULT_LAYOUT)
            inspections.append(
                FrameInspection(
                    packet=None,
                    status="frame-crc",
                    bits=damaged,
                    layout=DEFAULT_LAYOUT,
                )
            )

        recovered = _recover_packets_by_bit_voting(inspections)

        self.assertEqual(len(recovered), 1)
        self.assertEqual(recovered[0].to_bytes(), packet.to_bytes())

    def test_temporal_bit_vote_recovers_repeated_damaged_chunk_frames(self) -> None:
        import numpy as np

        from screenfile.chunking import build_packets_from_bytes
        from screenfile.cli import _recover_packets_by_temporal_bit_voting
        from screenfile.frame_codec import DEFAULT_LAYOUT, FrameInspection, _bits_to_packet, _packet_to_bits

        packet = build_packets_from_bytes("temporal.bin", b"temporal-payload" * 60, chunk_size=640)[0]
        clean_bits = _packet_to_bits(packet.to_bytes(), DEFAULT_LAYOUT)
        rng = np.random.default_rng(23)
        timed_inspections = []

        for frame_number in range(100, 107):
            damaged = clean_bits.copy()
            damaged[rng.choice(clean_bits.size, size=650, replace=False)] ^= 1
            with self.assertRaises(ValueError):
                _bits_to_packet(damaged, DEFAULT_LAYOUT)
            timed_inspections.append(
                (
                    frame_number,
                    FrameInspection(
                        packet=None,
                        status="frame-crc",
                        bits=damaged,
                        layout=DEFAULT_LAYOUT,
                    ),
                )
            )

        recovered = _recover_packets_by_temporal_bit_voting(timed_inspections, window_size=8)

        self.assertEqual(len(recovered), 1)
        self.assertEqual(recovered[0].to_bytes(), packet.to_bytes())

    def test_predicted_frame_numbers_cover_chunk_centers_with_offsets(self) -> None:
        from screenfile.cli import _predicted_frame_numbers

        frames = _predicted_frame_numbers(total_frames=100, total_chunks=10, offsets=(0, -1, 1))

        self.assertEqual(frames[0], 0)
        self.assertIn(10, frames)
        self.assertIn(11, frames)
        self.assertIn(99, frames)
        self.assertEqual(frames, sorted(set(frames)))


class VideoPipelineTests(unittest.TestCase):
    def test_video_pipeline_recovers_original_file(self) -> None:
        from screenfile.cli import decode_video_to_file, encode_file_to_video

        payload = (b"video-payload-" * 2000)[:1024 * 128]

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            source = tmp / "source.bin"
            video = tmp / "transfer.mp4"
            restored = tmp / "restored.bin"
            source.write_bytes(payload)

            encode_file_to_video(source, video, skip_confirmation=True)
            decode_video_to_file(video, restored)

            self.assertEqual(restored.read_bytes(), payload)

    def test_encode_prints_compression_summary(self) -> None:
        from screenfile.cli import encode_file_to_video

        payload = (b"compress-me-" * 4000)[:1024 * 64]

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            source = tmp / "source.bin"
            video = tmp / "transfer.mp4"
            source.write_bytes(payload)

            output = io.StringIO()
            with redirect_stdout(output):
                encode_file_to_video(source, video, compression="zstd", skip_confirmation=True)

            text = output.getvalue()
            self.assertIn("Compression: zstd", text)
            self.assertRegex(text, r"Original size: [0-9.]+ (KB|MB|GB|B) \([0-9,]+ B\)")
            self.assertRegex(text, r"Encoded size: [0-9.]+ (KB|MB|GB|B) \([0-9,]+ B\)")
            self.assertIn("Estimated duration:", text)
            self.assertRegex(text, r"Estimated video size: [0-9.]+ (MB|GB)")

    def test_estimate_prints_all_compression_profiles(self) -> None:
        from screenfile.cli import print_estimates

        payload = (b"estimate-me-" * 4000)[:1024 * 64]

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            source = tmp / "source.bin"
            source.write_bytes(payload)

            output = io.StringIO()
            with redirect_stdout(output):
                print_estimates(source, chunk_size=640, repeat=3, fps=8)

            text = output.getvalue()
            self.assertIn("Compression: none", text)
            self.assertIn("Compression: gzip", text)
            self.assertIn("Compression: zstd", text)
            self.assertIn("Estimated duration:", text)
            self.assertIn("Frames:", text)
            self.assertRegex(text, r"Estimated video size: [0-9.]+ (MB|GB)")

    def test_estimated_video_size_stays_reasonably_close_to_actual_output(self) -> None:
        from screenfile.cli import _estimate_for_compression, encode_file_to_video

        payload = (b"estimate-video-size-" * 12000)[:1024 * 192]

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            source = tmp / "source.bin"
            video = tmp / "transfer.mp4"
            source.write_bytes(payload)

            summary = _estimate_for_compression(
                source,
                chunk_size=640,
                repeat=3,
                fps=8,
                compression="none",
                output_suffix=".mp4",
            )
            encode_file_to_video(source, video, chunk_size=640, repeat=3, fps=8, compression="none", skip_confirmation=True)

            estimated = int(summary["estimated_video_size_bytes"])
            actual = video.stat().st_size
            ratio = actual / max(1, estimated)
            self.assertGreater(ratio, 0.5)
            self.assertLess(ratio, 1.8)

    def test_chunk_size_error_explains_payload_limit(self) -> None:
        from screenfile.cli import MAX_CHUNK_SIZE, encode_file_to_video

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            source = tmp / "source.bin"
            video = tmp / "transfer.mp4"
            source.write_bytes(b"payload")

            with self.assertRaisesRegex(
                ValueError,
                rf"chunk_size is too large.*{MAX_CHUNK_SIZE}.*Try --chunk-size {MAX_CHUNK_SIZE} or lower",
            ):
                encode_file_to_video(
                    source,
                    video,
                    chunk_size=MAX_CHUNK_SIZE + 1,
                    repeat=1,
                    fps=8,
                    compression="none",
                    skip_confirmation=True,
                )

    def test_encode_prompts_before_generating_by_default(self) -> None:
        from screenfile.cli import encode_file_to_video

        payload = (b"confirm-me-" * 4000)[:1024 * 32]

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            source = tmp / "source.bin"
            video = tmp / "transfer.mp4"
            source.write_bytes(payload)

            output = io.StringIO()
            with patch("builtins.input", return_value="n"), redirect_stdout(output):
                encode_file_to_video(source, video)

            self.assertFalse(video.exists())
            self.assertIn("Proceed with video generation?", output.getvalue())

    def test_encode_can_skip_confirmation(self) -> None:
        from screenfile.cli import encode_file_to_video

        payload = (b"skip-confirm-" * 4000)[:1024 * 32]

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            source = tmp / "source.bin"
            video = tmp / "transfer.mp4"
            source.write_bytes(payload)

            encode_file_to_video(source, video, skip_confirmation=True)

            self.assertTrue(video.exists())

    def test_encode_repeats_each_chunk_consecutively_for_voting(self) -> None:
        from screenfile.cli import encode_file_to_video
        from screenfile.frame_codec import decode_frame
        from screenfile.video_io import read_video_frames

        payload = bytes(range(256)) * 8

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            source = tmp / "source.bin"
            video = tmp / "transfer.mp4"
            source.write_bytes(payload)

            encode_file_to_video(
                source,
                video,
                chunk_size=512,
                repeat=2,
                fps=6,
                compression="none",
                skip_confirmation=True,
            )
            frames = read_video_frames(video)
            first = decode_frame(next(frames))
            second = decode_frame(next(frames))
            third = decode_frame(next(frames))

            self.assertIsNotNone(first)
            self.assertIsNotNone(second)
            self.assertIsNotNone(third)
            self.assertEqual(first.chunk_index, 0)
            self.assertEqual(second.chunk_index, 0)
            self.assertEqual(third.chunk_index, 1)

    def test_encode_can_generate_color_mode_video(self) -> None:
        from screenfile.cli import decode_video_to_file, encode_file_to_video

        payload = (b"color-video-" * 3000)[:1024 * 32]

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            source = tmp / "source.bin"
            video = tmp / "transfer.mp4"
            restored = tmp / "restored.bin"
            source.write_bytes(payload)

            encode_file_to_video(source, video, mode="color", chunk_size=768, repeat=2, fps=8, compression="none", skip_confirmation=True)
            decode_video_to_file(video, restored)

            self.assertEqual(restored.read_bytes(), payload)

    def test_decoder_reports_missing_chunks_when_frames_are_dropped(self) -> None:
        import cv2
        import numpy as np

        from screenfile.cli import decode_video_to_file, encode_file_to_video
        from screenfile.video_io import read_video_frames, write_video

        payload = (b"frame-loss-" * 2000)[:1024 * 64]

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            source = tmp / "source.bin"
            full_video = tmp / "full.mp4"
            lossy_video = tmp / "lossy.mp4"
            restored = tmp / "restored.bin"
            source.write_bytes(payload)

            encode_file_to_video(source, full_video, repeat=1, fps=6, compression="none", skip_confirmation=True)
            frames = list(read_video_frames(full_video))
            trimmed = frames[::2]
            write_video(lossy_video, trimmed, fps=6, frame_size=(frames[0].shape[1], frames[0].shape[0]))

            with self.assertRaisesRegex(RuntimeError, "Missing chunks"):
                decode_video_to_file(lossy_video, restored)

    def test_decoder_auto_skips_leading_and_trailing_noise_frames(self) -> None:
        import numpy as np

        from screenfile.cli import decode_video_to_file, encode_file_to_video
        from screenfile.video_io import read_video_frames, write_video

        payload = (b"segment-aware-" * 3000)[:1024 * 64]

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            source = tmp / "source.bin"
            clean_video = tmp / "clean.mp4"
            noisy_video = tmp / "noisy.mp4"
            restored = tmp / "restored.bin"
            source.write_bytes(payload)

            encode_file_to_video(source, clean_video, repeat=2, fps=6, compression="none", skip_confirmation=True)
            frames = list(read_video_frames(clean_video))
            noise_frame = np.full_like(frames[0], 240)
            combined = [noise_frame.copy() for _ in range(12)] + frames + [noise_frame.copy() for _ in range(15)]
            write_video(noisy_video, combined, fps=6, frame_size=(frames[0].shape[1], frames[0].shape[0]))

            decode_video_to_file(noisy_video, restored)

            self.assertEqual(restored.read_bytes(), payload)

    def test_decoder_reports_when_no_valid_data_frames_are_found(self) -> None:
        import numpy as np

        from screenfile.cli import decode_video_to_file
        from screenfile.video_io import write_video

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            video = tmp / "noise.mp4"
            restored = tmp / "restored.bin"
            frame = np.full((1080, 1920, 3), 240, dtype=np.uint8)
            write_video(video, [frame.copy() for _ in range(20)], fps=6, frame_size=(1920, 1080))

            with self.assertRaisesRegex(RuntimeError, "No valid data frames detected"):
                decode_video_to_file(video, restored)

    def test_decoder_writes_debug_samples_for_failed_frames(self) -> None:
        import numpy as np

        from screenfile.cli import decode_video_to_file
        from screenfile.video_io import write_video

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            video = tmp / "noise.mp4"
            restored = tmp / "restored.bin"
            debug_dir = tmp / "debug"
            frame = np.full((1080, 1920, 3), 240, dtype=np.uint8)
            write_video(video, [frame.copy() for _ in range(6)], fps=6, frame_size=(1920, 1080))

            with self.assertRaisesRegex(RuntimeError, "No valid data frames detected"):
                decode_video_to_file(video, restored, debug_dir=debug_dir, debug_limit=2)

            exported = sorted(path.name for path in debug_dir.iterdir())
            input_frames = [name for name in exported if name.endswith("-input.jpg")]
            self.assertEqual(len(input_frames), 2)
            self.assertIn("frame-00001-packet-invalid-binary.png", exported)
            self.assertIn("frame-00001-packet-invalid-square.png", exported)


class DemoAndPackagingTests(unittest.TestCase):
    def test_demo_roundtrip_creates_matching_files(self) -> None:
        from screenfile.demo import run_demo_roundtrip

        with tempfile.TemporaryDirectory() as tmpdir:
            result = run_demo_roundtrip(Path(tmpdir), payload_size=32768)

            self.assertTrue(result.source_path.exists())
            self.assertTrue(result.video_path.exists())
            self.assertTrue(result.restored_path.exists())
            self.assertEqual(result.source_path.read_bytes(), result.restored_path.read_bytes())

    def test_cli_main_accepts_explicit_argv(self) -> None:
        from screenfile.cli import main

        with self.assertRaises(SystemExit) as exc:
            main(["--help"])

        self.assertEqual(exc.exception.code, 0)

    def test_cli_version_flag_exits_cleanly(self) -> None:
        from screenfile.cli import main

        with self.assertRaises(SystemExit) as exc:
            main(["--version"])

        self.assertEqual(exc.exception.code, 0)

    def test_windows_build_script_exists(self) -> None:
        root = Path(__file__).resolve().parent.parent
        self.assertTrue((root / "scripts" / "build_windows.ps1").exists())
        self.assertTrue((root / "scripts" / "build_windows.bat").exists())

    def test_github_actions_workflow_exists(self) -> None:
        root = Path(__file__).resolve().parent.parent
        workflow = root / ".github" / "workflows" / "build-binaries.yml"
        self.assertTrue(workflow.exists())
        content = workflow.read_text(encoding="utf-8")
        self.assertIn("windows-latest", content)
        self.assertIn("macos-latest", content)
        self.assertIn("ubuntu-latest", content)
        self.assertIn("actions/upload-artifact", content)
        self.assertIn("screenfile-${version}-${{ matrix.os_name }}-${{ matrix.cpu_arch }}.zip", content)
        self.assertIn("cpu_arch: arm64", content)
        self.assertIn("cpu_arch: x64", content)

    def test_release_workflow_exists(self) -> None:
        root = Path(__file__).resolve().parent.parent
        workflow = root / ".github" / "workflows" / "release-binaries.yml"
        self.assertTrue(workflow.exists())
        content = workflow.read_text(encoding="utf-8")
        self.assertIn("softprops/action-gh-release", content)
        self.assertIn("tags", content)
        self.assertIn("screenfile-${GITHUB_REF_NAME}-${{ matrix.os_name }}-${{ matrix.cpu_arch }}.zip", content)
        self.assertIn("files: dist/screenfile-*.zip", content)


if __name__ == "__main__":
    unittest.main()
