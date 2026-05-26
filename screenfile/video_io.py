from __future__ import annotations

from pathlib import Path
from typing import Iterable, Iterator

import cv2
import numpy as np


def _fourcc_for_path(path: Path) -> int:
    if path.suffix.lower() == ".avi":
        return cv2.VideoWriter_fourcc(*"MJPG")
    return cv2.VideoWriter_fourcc(*"mp4v")


def write_video(
    output_path: Path,
    frames: Iterable[np.ndarray],
    fps: int,
    frame_size: tuple[int, int],
) -> None:
    writer = cv2.VideoWriter(str(output_path), _fourcc_for_path(output_path), float(fps), frame_size)
    if not writer.isOpened():
        raise RuntimeError(f"Could not open video writer for {output_path}")

    try:
        wrote = False
        for frame in frames:
            if frame.shape[1] != frame_size[0] or frame.shape[0] != frame_size[1]:
                raise ValueError("Frame size mismatch")
            writer.write(frame)
            wrote = True
        if not wrote:
            raise ValueError("No frames supplied")
    finally:
        writer.release()


def read_video_frames(input_path: Path) -> Iterator[np.ndarray]:
    capture = cv2.VideoCapture(str(input_path))
    if not capture.isOpened():
        raise RuntimeError(f"Could not open video file {input_path}")

    try:
        while True:
            ok, frame = capture.read()
            if not ok:
                break
            yield frame
    finally:
        capture.release()
