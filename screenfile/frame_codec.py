from __future__ import annotations

import struct
import zlib
from typing import Literal

import cv2
import numpy as np

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

    label = f"chunk {packet.chunk_index + 1}/{packet.total_chunks}"
    cv2.putText(frame, label, (80, FRAME_HEIGHT - 90), cv2.FONT_HERSHEY_SIMPLEX, 1.6, (0, 0, 0), 3, cv2.LINE_AA)
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


def _extract_square(frame: np.ndarray) -> np.ndarray:
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    blurred = cv2.GaussianBlur(gray, (5, 5), 0)
    _, binary = cv2.threshold(blurred, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    contours, _ = cv2.findContours(binary, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not contours:
        raise ValueError("No contour found")

    best_quad: np.ndarray | None = None
    best_area = 0.0
    for contour in contours:
        perimeter = cv2.arcLength(contour, True)
        approx = cv2.approxPolyDP(contour, 0.02 * perimeter, True)
        if len(approx) != 4:
            continue
        area = cv2.contourArea(approx)
        if area > best_area:
            best_area = area
            best_quad = approx.reshape(4, 2).astype(np.float32)

    if best_quad is None:
        raise ValueError("No quadrilateral contour found")

    ordered = _order_points(best_quad)
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


def decode_frame_with_status(frame: np.ndarray) -> tuple[Packet | None, Literal["ok", "no-quad", "frame-crc", "packet-invalid"]]:
    try:
        square = _extract_square(frame)
    except ValueError:
        return None, "no-quad"

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
            return None, "frame-crc"
        return None, "packet-invalid"

    try:
        return packet_from_bytes(packet_bytes), "ok"
    except ValueError:
        return None, "packet-invalid"


def decode_frame(frame: np.ndarray) -> Packet | None:
    packet, status = decode_frame_with_status(frame)
    return packet if status == "ok" else None
