from __future__ import annotations

import struct
import zlib
from dataclasses import dataclass
from typing import Literal

import cv2
import numpy as np

from screenfile import __version__
from screenfile.chunking import Packet, packet_from_bytes

FRAME_WIDTH = 1920
FRAME_HEIGHT = 1080
CODE_SQUARE = 912
QUIET_ZONE = 56
BORDER = 16
MARKER_SIZE = 88
GRID_SIZE = 96
CELL_SIZE = 8
DATA_SIZE = GRID_SIZE * CELL_SIZE
FRAME_HEADER = struct.Struct(">HI")
MAX_PACKET_BYTES = (GRID_SIZE * GRID_SIZE // 8) - FRAME_HEADER.size
PROTOCOL_LABEL = "layout=v1"


@dataclass(frozen=True)
class FrameInspection:
    packet: Packet | None
    status: Literal["ok", "no-quad", "frame-crc", "packet-invalid"]
    square: np.ndarray | None = None
    binary: np.ndarray | None = None


def _packet_to_bits(packet_bytes: bytes) -> np.ndarray:
    if len(packet_bytes) > MAX_PACKET_BYTES:
        raise ValueError(f"Packet too large for one frame: {len(packet_bytes)} > {MAX_PACKET_BYTES}")

    encoded = FRAME_HEADER.pack(len(packet_bytes), zlib.crc32(packet_bytes) & 0xFFFFFFFF) + packet_bytes
    padded = encoded + bytes((GRID_SIZE * GRID_SIZE // 8) - len(encoded))
    return np.unpackbits(np.frombuffer(padded, dtype=np.uint8))


def _bits_to_packet(bits: np.ndarray) -> bytes:
    raw = np.packbits(bits.astype(np.uint8)).tobytes()
    packet_size, packet_crc = FRAME_HEADER.unpack(raw[: FRAME_HEADER.size])
    if packet_size <= 0 or packet_size > MAX_PACKET_BYTES:
        raise ValueError("Decoded packet size is invalid")
    packet_bytes = raw[FRAME_HEADER.size : FRAME_HEADER.size + packet_size]
    if zlib.crc32(packet_bytes) & 0xFFFFFFFF != packet_crc:
        raise ValueError("Frame CRC mismatch")
    return packet_bytes


def _draw_marker(canvas: np.ndarray, x: int, y: int) -> None:
    cv2.rectangle(canvas, (x, y), (x + MARKER_SIZE, y + MARKER_SIZE), (0, 0, 0), -1)
    cv2.rectangle(canvas, (x + 12, y + 12), (x + MARKER_SIZE - 12, y + MARKER_SIZE - 12), (255, 255, 255), -1)
    cv2.rectangle(canvas, (x + 24, y + 24), (x + MARKER_SIZE - 24, y + MARKER_SIZE - 24), (0, 0, 0), -1)


def _draw_corner_label(
    frame: np.ndarray,
    text: str,
    *,
    anchor: tuple[int, int],
    align: Literal["left", "right"],
) -> None:
    font = cv2.FONT_HERSHEY_SIMPLEX
    scale = 0.78
    thickness = 2
    padding_x = 14
    padding_y = 10
    (text_width, text_height), baseline = cv2.getTextSize(text, font, scale, thickness)
    anchor_x, anchor_y = anchor
    if align == "left":
        box_left = anchor_x
        text_x = box_left + padding_x
    else:
        box_left = anchor_x - (text_width + padding_x * 2)
        text_x = box_left + padding_x
    box_top = anchor_y
    box_right = box_left + text_width + padding_x * 2
    box_bottom = box_top + text_height + padding_y * 2 + baseline
    cv2.rectangle(frame, (box_left, box_top), (box_right, box_bottom), (255, 255, 255), -1)
    cv2.rectangle(frame, (box_left, box_top), (box_right, box_bottom), (0, 0, 0), 2)
    text_y = box_top + padding_y + text_height
    cv2.putText(frame, text, (text_x, text_y), font, scale, (0, 0, 0), thickness, cv2.LINE_AA)


def encode_packet_frame(packet: Packet) -> np.ndarray:
    frame = np.full((FRAME_HEIGHT, FRAME_WIDTH, 3), 255, dtype=np.uint8)
    square = np.full((CODE_SQUARE, CODE_SQUARE, 3), 255, dtype=np.uint8)

    cv2.rectangle(square, (0, 0), (CODE_SQUARE - 1, CODE_SQUARE - 1), (0, 0, 0), BORDER)
    _draw_marker(square, QUIET_ZONE, QUIET_ZONE)
    _draw_marker(square, CODE_SQUARE - QUIET_ZONE - MARKER_SIZE, QUIET_ZONE)
    _draw_marker(square, QUIET_ZONE, CODE_SQUARE - QUIET_ZONE - MARKER_SIZE)
    _draw_marker(square, CODE_SQUARE - QUIET_ZONE - MARKER_SIZE, CODE_SQUARE - QUIET_ZONE - MARKER_SIZE)

    bits = _packet_to_bits(packet.to_bytes()).reshape(GRID_SIZE, GRID_SIZE)
    bit_image = np.where(bits == 1, 0, 255).astype(np.uint8)
    bit_image = np.repeat(np.repeat(bit_image, CELL_SIZE, axis=0), CELL_SIZE, axis=1)
    x0 = (CODE_SQUARE - DATA_SIZE) // 2
    y0 = x0
    square[y0 : y0 + DATA_SIZE, x0 : x0 + DATA_SIZE] = cv2.cvtColor(bit_image, cv2.COLOR_GRAY2BGR)

    start_x = (FRAME_WIDTH - CODE_SQUARE) // 2
    start_y = (FRAME_HEIGHT - CODE_SQUARE) // 2
    frame[start_y : start_y + CODE_SQUARE, start_x : start_x + CODE_SQUARE] = square

    version_label = f"screenfile {__version__} {PROTOCOL_LABEL}"
    chunk_label = f"chunk {packet.chunk_index + 1}/{packet.total_chunks}"
    _draw_corner_label(frame, version_label, anchor=(36, 18), align="left")
    _draw_corner_label(frame, chunk_label, anchor=(FRAME_WIDTH - 36, 18), align="right")
    return frame


def _order_points(points: np.ndarray) -> np.ndarray:
    rect = np.zeros((4, 2), dtype=np.float32)
    s = points.sum(axis=1)
    rect[0] = points[np.argmin(s)]
    rect[2] = points[np.argmax(s)]
    diff = np.diff(points, axis=1)
    rect[1] = points[np.argmin(diff)]
    rect[3] = points[np.argmax(diff)]
    return rect


def _warp_quad(frame: np.ndarray, quad: np.ndarray) -> np.ndarray:
    ordered = _order_points(quad)
    destination = np.array(
        [
            [0, 0],
            [CODE_SQUARE - 1, 0],
            [CODE_SQUARE - 1, CODE_SQUARE - 1],
            [0, CODE_SQUARE - 1],
        ],
        dtype=np.float32,
    )
    matrix = cv2.getPerspectiveTransform(ordered, destination)
    return cv2.warpPerspective(frame, matrix, (CODE_SQUARE, CODE_SQUARE), borderValue=(255, 255, 255))


def _quad_square_score(quad: np.ndarray) -> float:
    _, (width, height), _ = cv2.minAreaRect(quad.astype(np.float32))
    if width <= 0 or height <= 0:
        return 0.0
    return min(width, height) / max(width, height)


def _score_warped_square(square: np.ndarray, *, area_ratio: float, square_score: float) -> float:
    gray = cv2.cvtColor(square, cv2.COLOR_BGR2GRAY)
    band = max(8, CODE_SQUARE // 36)
    border_mask = np.zeros(gray.shape, dtype=bool)
    border_mask[:band, :] = True
    border_mask[-band:, :] = True
    border_mask[:, :band] = True
    border_mask[:, -band:] = True

    border_mean = float(gray[border_mask].mean())
    center = gray[CODE_SQUARE // 4 : (CODE_SQUARE * 3) // 4, CODE_SQUARE // 4 : (CODE_SQUARE * 3) // 4]
    center_mean = float(center.mean())
    contrast = max(center_mean - border_mean, 0.0) / 255.0
    border_darkness = (255.0 - border_mean) / 255.0
    center_brightness = center_mean / 255.0
    return (
        square_score * 3.0
        + border_darkness * 2.0
        + center_brightness * 1.0
        + contrast * 1.5
        + area_ratio * 0.35
    )


def _candidate_quads(mask: np.ndarray) -> list[np.ndarray]:
    contours, _ = cv2.findContours(mask, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
    candidates: list[np.ndarray] = []
    for contour in contours:
        perimeter = cv2.arcLength(contour, True)
        if perimeter <= 0:
            continue
        approx = cv2.approxPolyDP(contour, 0.02 * perimeter, True)
        if len(approx) == 4:
            candidates.append(approx.reshape(4, 2).astype(np.float32))
    return candidates


def _extract_square(frame: np.ndarray) -> np.ndarray:
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    blurred = cv2.GaussianBlur(gray, (5, 5), 0)
    _, binary = cv2.threshold(blurred, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    edges = cv2.Canny(blurred, 50, 150)
    edges = cv2.dilate(edges, np.ones((3, 3), dtype=np.uint8), iterations=1)
    candidates = _candidate_quads(binary) + _candidate_quads(edges)
    if not candidates:
        raise ValueError("No contour found")

    frame_area = float(frame.shape[0] * frame.shape[1])
    best_square: np.ndarray | None = None
    best_score = float("-inf")

    for quad in candidates:
        area = cv2.contourArea(quad)
        if area < frame_area * 0.01:
            continue

        square_score = _quad_square_score(quad)
        if square_score < 0.72:
            continue

        warped = _warp_quad(frame, quad)
        score = _score_warped_square(warped, area_ratio=area / frame_area, square_score=square_score)
        if score > best_score:
            best_score = score
            best_square = warped

    if best_square is None:
        raise ValueError("No quadrilateral contour found")
    return best_square


def inspect_frame(frame: np.ndarray) -> FrameInspection:
    try:
        square = _extract_square(frame)
    except ValueError:
        return FrameInspection(packet=None, status="no-quad")

    gray = cv2.cvtColor(square, cv2.COLOR_BGR2GRAY)
    _, binary = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    start = (CODE_SQUARE - DATA_SIZE) // 2
    data_region = binary[start : start + DATA_SIZE, start : start + DATA_SIZE]
    cells = data_region.reshape(GRID_SIZE, CELL_SIZE, GRID_SIZE, CELL_SIZE).mean(axis=(1, 3))
    bits = (cells < 127).astype(np.uint8).reshape(-1)

    try:
        packet_bytes = _bits_to_packet(bits)
    except ValueError as exc:
        message = str(exc)
        if "CRC" in message:
            return FrameInspection(packet=None, status="frame-crc", square=square, binary=binary)
        return FrameInspection(packet=None, status="packet-invalid", square=square, binary=binary)

    try:
        return FrameInspection(packet=packet_from_bytes(packet_bytes), status="ok", square=square, binary=binary)
    except ValueError:
        return FrameInspection(packet=None, status="packet-invalid", square=square, binary=binary)


def decode_frame_with_status(frame: np.ndarray) -> tuple[Packet | None, Literal["ok", "no-quad", "frame-crc", "packet-invalid"]]:
    inspection = inspect_frame(frame)
    return inspection.packet, inspection.status


def decode_frame(frame: np.ndarray) -> Packet | None:
    packet, status = decode_frame_with_status(frame)
    return packet if status == "ok" else None
