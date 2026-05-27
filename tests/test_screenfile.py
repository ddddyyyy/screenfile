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
        from screenfile.frame_codec import CODE_SQUARE, FRAME_HEIGHT, FRAME_WIDTH, PROTOCOL_LABEL, encode_packet_frame

        packet = build_packets_from_bytes("frame.bin", b"x" * 300, chunk_size=300)[0]
        frame = encode_packet_frame(packet)

        code_start_x = (FRAME_WIDTH - CODE_SQUARE) // 2
        code_start_y = (FRAME_HEIGHT - CODE_SQUARE) // 2
        top_margin = frame[:code_start_y, :, :]
        left_label_region = top_margin[:, : code_start_x - 12, :]
        right_label_region = top_margin[:, code_start_x + CODE_SQUARE + 12 :, :]
        center_top_region = top_margin[:, code_start_x + 20 : code_start_x + CODE_SQUARE - 20, :]

        self.assertLess(left_label_region.mean(), 250)
        self.assertLess(right_label_region.mean(), 250)
        self.assertGreater(center_top_region.mean(), 250)
        self.assertEqual(PROTOCOL_LABEL, "layout=v1")

    def test_decoder_handles_mild_camera_like_degradation(self) -> None:
        import cv2
        import numpy as np

        from screenfile.chunking import build_packets_from_bytes
        from screenfile.frame_codec import decode_frame, encode_packet_frame

        packet = build_packets_from_bytes("camera.bin", b"payload" * 80, chunk_size=400)[0]
        frame = encode_packet_frame(packet)

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

        scaled = cv2.resize(frame, (980, 580), interpolation=cv2.INTER_AREA)
        y0, x0 = 610, 420
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
                [470, 640],
                [1405, 585],
                [1460, 1160],
                [380, 1215],
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
            self.assertEqual(len(exported), 2)
            self.assertTrue(all(name.endswith("-input.jpg") for name in exported))


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
        self.assertIn("screenfile-windows-x64.zip", content)
        self.assertIn("screenfile-linux-x64.zip", content)
        self.assertIn("screenfile-macos", content)

    def test_release_workflow_exists(self) -> None:
        root = Path(__file__).resolve().parent.parent
        workflow = root / ".github" / "workflows" / "release-binaries.yml"
        self.assertTrue(workflow.exists())
        content = workflow.read_text(encoding="utf-8")
        self.assertIn("softprops/action-gh-release", content)
        self.assertIn("tags", content)
        self.assertIn("screenfile-windows-x64.zip", content)


if __name__ == "__main__":
    unittest.main()
