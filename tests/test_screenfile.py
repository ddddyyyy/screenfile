from __future__ import annotations

import tempfile
import unittest
from pathlib import Path


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


class FrameCodecTests(unittest.TestCase):
    def test_frame_encode_decode_round_trip(self) -> None:
        from screenfile.chunking import build_packets_from_bytes
        from screenfile.frame_codec import decode_frame, encode_packet_frame

        packet = build_packets_from_bytes("frame.bin", b"x" * 300, chunk_size=300)[0]
        frame = encode_packet_frame(packet)
        restored = decode_frame(frame)

        self.assertIsNotNone(restored)
        self.assertEqual(restored.to_bytes(), packet.to_bytes())

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

            encode_file_to_video(source, video)
            decode_video_to_file(video, restored)

            self.assertEqual(restored.read_bytes(), payload)

    def test_decoder_reports_missing_chunks_when_frames_are_dropped(self) -> None:
        import cv2

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

            encode_file_to_video(source, full_video, repeat=1, fps=6)
            frames = list(read_video_frames(full_video))
            trimmed = frames[::2]
            write_video(lossy_video, trimmed, fps=6, frame_size=(frames[0].shape[1], frames[0].shape[0]))

            with self.assertRaisesRegex(RuntimeError, "Missing chunks"):
                decode_video_to_file(lossy_video, restored)


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
