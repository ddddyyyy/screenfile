from __future__ import annotations

import struct
import zlib
from dataclasses import dataclass
from typing import Literal

import cv2
import numpy as np
from reedsolo import ReedSolomonError, RSCodec

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
FRAME_RS_BYTES = 32
FRAME_RS_DATA_BYTES = 223
FRAME_RS_BLOCK_BYTES = FRAME_RS_DATA_BYTES + FRAME_RS_BYTES
FRAME_RS_CODEC = RSCodec(FRAME_RS_BYTES)


@dataclass(frozen=True)
class FrameLayout:
    label: str
    code_width: int
    code_height: int
    grid_cols: int
    grid_rows: int
    cell_size: int
    bits_per_cell: int = 1

    @property
    def data_width(self) -> int:
        return self.grid_cols * self.cell_size

    @property
    def data_height(self) -> int:
        return self.grid_rows * self.cell_size

    @property
    def data_bits(self) -> int:
        return self.grid_cols * self.grid_rows * self.bits_per_cell

    @property
    def capacity_bytes(self) -> int:
        return (self.data_bits // 8) - FRAME_HEADER.size

    @property
    def max_rs_blocks(self) -> int:
        return self.capacity_bytes // FRAME_RS_BLOCK_BYTES

    @property
    def max_packet_bytes(self) -> int:
        return FRAME_RS_DATA_BYTES * self.max_rs_blocks

    @property
    def aspect_ratio(self) -> float:
        return self.code_width / self.code_height


LAYOUT_V1 = FrameLayout("layout=v1", CODE_SQUARE, CODE_SQUARE, GRID_SIZE, GRID_SIZE, CELL_SIZE)
LAYOUT_V2 = FrameLayout("layout=v2", 1728, 960, 160, 80, 10)
LAYOUT_V3 = FrameLayout("layout=v3", 1728, 960, 144, 72, 11)
COLOR_LAYOUT = FrameLayout("color-v1", 1728, 960, 96, 54, 16, 2)
DEFAULT_LAYOUT = LAYOUT_V3
DECODE_LAYOUTS = (COLOR_LAYOUT, LAYOUT_V3, LAYOUT_V2, LAYOUT_V1)

FRAME_CAPACITY_BYTES = DEFAULT_LAYOUT.capacity_bytes
MAX_RS_BLOCKS = DEFAULT_LAYOUT.max_rs_blocks
MAX_PACKET_BYTES = DEFAULT_LAYOUT.max_packet_bytes
PROTOCOL_LABEL = DEFAULT_LAYOUT.label

COLOR_PALETTE_BGR = np.array(
    [
        [0, 0, 0],
        [0, 0, 255],
        [255, 0, 0],
        [0, 180, 0],
    ],
    dtype=np.uint8,
)
COLOR_PALETTE_LAB = cv2.cvtColor(COLOR_PALETTE_BGR.reshape(1, 4, 3), cv2.COLOR_BGR2LAB).reshape(4, 3).astype(np.float32)


@dataclass(frozen=True)
class FrameInspection:
    packet: Packet | None
    status: Literal["ok", "no-quad", "frame-crc", "packet-invalid"]
    square: np.ndarray | None = None
    binary: np.ndarray | None = None
    bits: np.ndarray | None = None
    layout: FrameLayout | None = None


def _packet_to_bits(packet_bytes: bytes, layout: FrameLayout = DEFAULT_LAYOUT) -> np.ndarray:
    if len(packet_bytes) > layout.max_packet_bytes:
        raise ValueError(f"Packet too large for one frame: {len(packet_bytes)} > {layout.max_packet_bytes}")

    encoded_blocks = []
    for start in range(0, len(packet_bytes), FRAME_RS_DATA_BYTES):
        block = packet_bytes[start : start + FRAME_RS_DATA_BYTES]
        padded = block + bytes(FRAME_RS_DATA_BYTES - len(block))
        encoded_blocks.append(bytes(FRAME_RS_CODEC.encode(padded)))
    encoded_payload = b"".join(encoded_blocks)
    encoded = FRAME_HEADER.pack(len(packet_bytes), zlib.crc32(packet_bytes) & 0xFFFFFFFF) + encoded_payload
    padded = encoded + bytes((layout.data_bits // 8) - len(encoded))
    return np.unpackbits(np.frombuffer(padded, dtype=np.uint8))


def _bits_to_packet(bits: np.ndarray, layout: FrameLayout = DEFAULT_LAYOUT) -> bytes:
    raw = np.packbits(bits.astype(np.uint8)).tobytes()
    packet_size, packet_crc = FRAME_HEADER.unpack(raw[: FRAME_HEADER.size])
    if packet_size <= 0 or packet_size > layout.max_packet_bytes:
        raise ValueError("Decoded packet size is invalid")
    block_count = (packet_size + FRAME_RS_DATA_BYTES - 1) // FRAME_RS_DATA_BYTES
    rs_size = block_count * FRAME_RS_BLOCK_BYTES
    rs_payload = raw[FRAME_HEADER.size : FRAME_HEADER.size + rs_size]
    if len(rs_payload) == rs_size:
        decoded_blocks = []
        try:
            for index in range(block_count):
                start = index * FRAME_RS_BLOCK_BYTES
                block = rs_payload[start : start + FRAME_RS_BLOCK_BYTES]
                decoded = FRAME_RS_CODEC.decode(block)
                decoded_blocks.append(bytes(decoded[0] if isinstance(decoded, tuple) else decoded))
            packet_bytes = b"".join(decoded_blocks)[:packet_size]
        except ReedSolomonError:
            packet_bytes = raw[FRAME_HEADER.size : FRAME_HEADER.size + packet_size]
    else:
        packet_bytes = raw[FRAME_HEADER.size : FRAME_HEADER.size + packet_size]

    if len(packet_bytes) != packet_size:
        raise ValueError("Decoded packet size is invalid")
    if zlib.crc32(packet_bytes) & 0xFFFFFFFF != packet_crc:
        raise ValueError("Frame CRC mismatch")
    return packet_bytes


def _packet_to_color_symbols(packet_bytes: bytes, layout: FrameLayout = COLOR_LAYOUT) -> np.ndarray:
    bits = _packet_to_bits(packet_bytes, layout)
    pairs = bits.reshape(-1, 2)
    return (pairs[:, 0] * 2 + pairs[:, 1]).astype(np.uint8)


def _color_symbols_to_bits(symbols: np.ndarray) -> np.ndarray:
    bits = np.empty(symbols.size * 2, dtype=np.uint8)
    bits[0::2] = (symbols >> 1) & 1
    bits[1::2] = symbols & 1
    return bits


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


def encode_packet_frame(packet: Packet, layout: FrameLayout = DEFAULT_LAYOUT, *, mode: Literal["matrix", "color"] = "matrix") -> np.ndarray:
    if mode == "color":
        layout = COLOR_LAYOUT
    frame = np.full((FRAME_HEIGHT, FRAME_WIDTH, 3), 255, dtype=np.uint8)
    code = np.full((layout.code_height, layout.code_width, 3), 255, dtype=np.uint8)

    cv2.rectangle(code, (0, 0), (layout.code_width - 1, layout.code_height - 1), (0, 0, 0), BORDER)
    _draw_marker(code, QUIET_ZONE, QUIET_ZONE)
    _draw_marker(code, layout.code_width - QUIET_ZONE - MARKER_SIZE, QUIET_ZONE)
    _draw_marker(code, QUIET_ZONE, layout.code_height - QUIET_ZONE - MARKER_SIZE)
    _draw_marker(code, layout.code_width - QUIET_ZONE - MARKER_SIZE, layout.code_height - QUIET_ZONE - MARKER_SIZE)

    if mode == "color":
        symbols = _packet_to_color_symbols(packet.to_bytes(), layout).reshape(layout.grid_rows, layout.grid_cols)
        cell_image = COLOR_PALETTE_BGR[symbols]
        bit_image = np.repeat(np.repeat(cell_image, layout.cell_size, axis=0), layout.cell_size, axis=1)
    else:
        bits = _packet_to_bits(packet.to_bytes(), layout).reshape(layout.grid_rows, layout.grid_cols)
        cell_image = np.where(bits == 1, 0, 255).astype(np.uint8)
        bit_image = np.repeat(np.repeat(cell_image, layout.cell_size, axis=0), layout.cell_size, axis=1)
        bit_image = cv2.cvtColor(bit_image, cv2.COLOR_GRAY2BGR)
    x0 = (layout.code_width - layout.data_width) // 2
    y0 = (layout.code_height - layout.data_height) // 2
    code[y0 : y0 + layout.data_height, x0 : x0 + layout.data_width] = bit_image

    start_x = (FRAME_WIDTH - layout.code_width) // 2
    start_y = (FRAME_HEIGHT - layout.code_height) // 2
    frame[start_y : start_y + layout.code_height, start_x : start_x + layout.code_width] = code

    version_label = f"screenfile {__version__} {layout.label}"
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


def _warp_quad_to_size(frame: np.ndarray, quad: np.ndarray, width: int, height: int) -> np.ndarray:
    ordered = _order_points(quad)
    destination = np.array(
        [
            [0, 0],
            [width - 1, 0],
            [width - 1, height - 1],
            [0, height - 1],
        ],
        dtype=np.float32,
    )
    matrix = cv2.getPerspectiveTransform(ordered, destination)
    return cv2.warpPerspective(frame, matrix, (width, height), borderValue=(255, 255, 255))


def _warp_quad(frame: np.ndarray, quad: np.ndarray, layout: FrameLayout) -> np.ndarray:
    return _warp_quad_to_size(frame, quad, layout.code_width, layout.code_height)


def _expand_quad(quad: np.ndarray, *, scale_x: float, scale_y: float) -> np.ndarray:
    ordered = _order_points(quad).astype(np.float32)
    center = ordered.mean(axis=0)
    expanded = ordered.copy()
    expanded[:, 0] = center[0] + (expanded[:, 0] - center[0]) * scale_x
    expanded[:, 1] = center[1] + (expanded[:, 1] - center[1]) * scale_y
    return expanded


def _quad_aspect_score(quad: np.ndarray, layout: FrameLayout) -> float:
    return _quad_ratio_score(quad, layout.aspect_ratio)


def _quad_ratio_score(quad: np.ndarray, target_ratio: float) -> float:
    _, (width, height), _ = cv2.minAreaRect(quad.astype(np.float32))
    if width <= 0 or height <= 0:
        return 0.0
    observed = max(width, height) / min(width, height)
    target = max(target_ratio, 1 / target_ratio)
    return min(observed, target) / max(observed, target)


def _score_warped_code(code: np.ndarray, layout: FrameLayout, *, area_ratio: float, aspect_score: float) -> float:
    gray = cv2.cvtColor(code, cv2.COLOR_BGR2GRAY)
    band = max(8, min(layout.code_width, layout.code_height) // 36)
    border_mask = np.zeros(gray.shape, dtype=bool)
    border_mask[:band, :] = True
    border_mask[-band:, :] = True
    border_mask[:, :band] = True
    border_mask[:, -band:] = True

    border_mean = float(gray[border_mask].mean())
    center = gray[
        layout.code_height // 4 : (layout.code_height * 3) // 4,
        layout.code_width // 4 : (layout.code_width * 3) // 4,
    ]
    center_mean = float(center.mean())
    contrast = max(center_mean - border_mean, 0.0) / 255.0
    border_darkness = (255.0 - border_mean) / 255.0
    center_brightness = center_mean / 255.0
    return (
        aspect_score * 3.0
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


def _likely_color_code_candidates(frame: np.ndarray, *, max_candidates: int = 6) -> list[np.ndarray]:
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    blurred = cv2.GaussianBlur(gray, (5, 5), 0)
    _, binary = cv2.threshold(blurred, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    edges = cv2.Canny(blurred, 50, 150)
    edges = cv2.dilate(edges, np.ones((3, 3), dtype=np.uint8), iterations=1)
    quads = _candidate_quads(binary) + _candidate_quads(edges)
    frame_area = float(frame.shape[0] * frame.shape[1])
    scored: list[tuple[float, np.ndarray]] = []

    for quad in quads:
        area = cv2.contourArea(quad)
        area_ratio = area / frame_area
        if area_ratio < 0.25 or area_ratio > 0.75:
            continue

        aspect_score = _quad_aspect_score(quad, COLOR_LAYOUT)
        if aspect_score < 0.75:
            continue

        x, y, width, height = cv2.boundingRect(quad.astype(np.int32))
        observed_ratio = width / max(height, 1)
        center_x = x + width / 2
        center_y = y + height / 2
        score = (
            aspect_score * 5.0
            - abs(area_ratio - 0.53) * 4.0
            - abs(observed_ratio - COLOR_LAYOUT.aspect_ratio) * 1.2
            - abs(center_x - frame.shape[1] / 2) / frame.shape[1]
            - abs(center_y - frame.shape[0] / 2) / frame.shape[0]
        )
        scored.append((score, quad))

    scored.sort(key=lambda item: item[0], reverse=True)
    candidates = [_warp_quad(frame, quad, COLOR_LAYOUT) for _score, quad in scored[:max_candidates]]

    if frame.shape[1] >= COLOR_LAYOUT.code_width and frame.shape[0] >= COLOR_LAYOUT.code_height:
        x0 = (frame.shape[1] - COLOR_LAYOUT.code_width) // 2
        y0 = (frame.shape[0] - COLOR_LAYOUT.code_height) // 2
        candidates.append(frame[y0 : y0 + COLOR_LAYOUT.code_height, x0 : x0 + COLOR_LAYOUT.code_width])
    return candidates


def decode_color_frame_fast(frame: np.ndarray) -> Packet | None:
    for code in _likely_color_code_candidates(frame):
        inspection = inspect_color_square(code, COLOR_LAYOUT)
        if inspection.status == "ok":
            return inspection.packet
    return None


def _direct_code_candidates_from_quads(
    frame: np.ndarray,
    quads: list[np.ndarray],
    *,
    min_area_ratio: float,
    max_area_ratio: float,
) -> list[tuple[float, FrameLayout, np.ndarray]]:
    frame_area = float(frame.shape[0] * frame.shape[1])
    scored: list[tuple[float, FrameLayout, np.ndarray]] = []

    for quad in quads:
        area = cv2.contourArea(quad)
        if area < frame_area * min_area_ratio:
            continue
        if area > frame_area * max_area_ratio:
            continue

        for layout in DECODE_LAYOUTS:
            aspect_score = _quad_aspect_score(quad, layout)
            if aspect_score < 0.68:
                continue

            quad_variants = [quad]
            if layout.bits_per_cell > 1:
                quad_variants.append(
                    _expand_quad(
                        quad,
                        scale_x=layout.code_width / layout.data_width,
                        scale_y=layout.code_height / layout.data_height,
                    )
                )

            for variant_index, quad_variant in enumerate(quad_variants):
                warped = _warp_quad(frame, quad_variant, layout)
                score = _score_warped_code(warped, layout, area_ratio=area / frame_area, aspect_score=aspect_score)
                if variant_index:
                    score += 0.35
                scored.append((score, layout, warped))
    return scored


def _extract_code_candidates(frame: np.ndarray) -> list[tuple[FrameLayout, np.ndarray]]:
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    blurred = cv2.GaussianBlur(gray, (5, 5), 0)
    _, binary = cv2.threshold(blurred, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    edges = cv2.Canny(blurred, 50, 150)
    edges = cv2.dilate(edges, np.ones((3, 3), dtype=np.uint8), iterations=1)
    candidates = _candidate_quads(binary) + _candidate_quads(edges)
    if not candidates:
        raise ValueError("No contour found")

    frame_area = float(frame.shape[0] * frame.shape[1])
    direct_scored: list[tuple[float, FrameLayout, np.ndarray]] = []
    full_frame_scored: list[tuple[float, FrameLayout, np.ndarray]] = []

    for quad in candidates:
        area = cv2.contourArea(quad)
        if area < frame_area * 0.01:
            continue
        if area > frame_area * 0.92:
            continue

        full_frame_score = _quad_ratio_score(quad, FRAME_WIDTH / FRAME_HEIGHT)
        if full_frame_score >= 0.76:
            full_frame = _warp_quad_to_size(frame, quad, FRAME_WIDTH, FRAME_HEIGHT)
            for layout, code in _centered_code_candidates(full_frame):
                score = _score_warped_code(
                    code,
                    layout,
                    area_ratio=area / frame_area,
                    aspect_score=full_frame_score,
                ) + 0.25
                full_frame_scored.append((score, layout, code))

            nested_gray = cv2.cvtColor(full_frame, cv2.COLOR_BGR2GRAY)
            nested_blurred = cv2.GaussianBlur(nested_gray, (5, 5), 0)
            _, nested_binary = cv2.threshold(
                nested_blurred,
                0,
                255,
                cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU,
            )
            nested_edges = cv2.Canny(nested_blurred, 50, 150)
            nested_edges = cv2.dilate(nested_edges, np.ones((3, 3), dtype=np.uint8), iterations=1)
            nested_quads = _candidate_quads(nested_binary) + _candidate_quads(nested_edges)
            for nested_score, nested_layout, nested_code in _direct_code_candidates_from_quads(
                full_frame,
                nested_quads,
                min_area_ratio=0.05,
                max_area_ratio=0.90,
            ):
                full_frame_scored.append((nested_score + 0.5, nested_layout, nested_code))

    direct_scored.extend(
        _direct_code_candidates_from_quads(
            frame,
            candidates,
            min_area_ratio=0.01,
            max_area_ratio=0.92,
        )
    )

    if not direct_scored and not full_frame_scored:
        raise ValueError("No quadrilateral contour found")
    direct_scored.sort(key=lambda item: item[0], reverse=True)
    full_frame_scored.sort(key=lambda item: item[0], reverse=True)
    scored = direct_scored[:40] + full_frame_scored[:40]
    return [(layout, warped) for _, layout, warped in scored]


def _centered_code_candidates(frame: np.ndarray) -> list[tuple[FrameLayout, np.ndarray]]:
    height, width = frame.shape[:2]
    candidates: list[tuple[FrameLayout, np.ndarray]] = []
    for layout in DECODE_LAYOUTS:
        if width < layout.code_width or height < layout.code_height:
            continue
        x0 = (width - layout.code_width) // 2
        y0 = (height - layout.code_height) // 2
        candidates.append((layout, frame[y0 : y0 + layout.code_height, x0 : x0 + layout.code_width]))
    return candidates


def _crop_layout_from_full_frame(frame: np.ndarray, layout: FrameLayout) -> np.ndarray:
    x0 = round((FRAME_WIDTH - layout.code_width) / 2 / FRAME_WIDTH * frame.shape[1])
    y0 = round((FRAME_HEIGHT - layout.code_height) / 2 / FRAME_HEIGHT * frame.shape[0])
    x1 = round((FRAME_WIDTH + layout.code_width) / 2 / FRAME_WIDTH * frame.shape[1])
    y1 = round((FRAME_HEIGHT + layout.code_height) / 2 / FRAME_HEIGHT * frame.shape[0])
    return cv2.resize(frame[y0:y1, x0:x1], (layout.code_width, layout.code_height), interpolation=cv2.INTER_AREA)


def _cell_bits_from_binary(binary: np.ndarray, layout: FrameLayout) -> tuple[np.ndarray, np.ndarray]:
    x0 = (layout.code_width - layout.data_width) // 2
    y0 = (layout.code_height - layout.data_height) // 2
    data_region = binary[y0 : y0 + layout.data_height, x0 : x0 + layout.data_width]
    cells = data_region.reshape(layout.grid_rows, layout.cell_size, layout.grid_cols, layout.cell_size).mean(axis=(1, 3))
    bits = (cells < 127).astype(np.uint8).reshape(-1)
    return bits, cells


def _cell_bits_from_color_rect(
    code: np.ndarray,
    layout: FrameLayout,
    *,
    x: int,
    y: int,
    width: int,
    height: int,
) -> tuple[np.ndarray, np.ndarray]:
    if x < 0 or y < 0 or x + width > code.shape[1] or y + height > code.shape[0]:
        raise ValueError("Color data region is outside the frame")
    data_region = code[y : y + height, x : x + width]
    if data_region.shape[:2] != (layout.data_height, layout.data_width):
        data_region = cv2.resize(
            data_region,
            (layout.data_width, layout.data_height),
            interpolation=cv2.INTER_AREA,
        )
    lab = cv2.cvtColor(data_region, cv2.COLOR_BGR2LAB).astype(np.float32)
    cells = lab.reshape(layout.grid_rows, layout.cell_size, layout.grid_cols, layout.cell_size, 3).mean(axis=(1, 3))
    distances = ((cells[:, :, None, :] - COLOR_PALETTE_LAB[None, None, :, :]) ** 2).sum(axis=3)
    symbols = distances.argmin(axis=2).astype(np.uint8).reshape(-1)
    bits = _color_symbols_to_bits(symbols)
    confidence = distances.min(axis=2)
    return bits, confidence


def _cell_bits_variants_from_color(code: np.ndarray, layout: FrameLayout = COLOR_LAYOUT) -> list[tuple[np.ndarray, np.ndarray]]:
    x0 = (layout.code_width - layout.data_width) // 2
    y0 = (layout.code_height - layout.data_height) // 2
    rects = [
        (x0, y0, layout.data_width, layout.data_height),
        (x0 + 8, y0 + 6, layout.data_width, layout.data_height),
        (x0 - 8, y0 - 6, layout.data_width, layout.data_height),
        (x0 + 8, y0 - 6, layout.data_width, layout.data_height),
        (x0 - 8, y0 + 6, layout.data_width, layout.data_height),
        (x0, y0 + 8, layout.data_width, layout.data_height),
        (x0, y0 - 8, layout.data_width, layout.data_height),
        (x0 + 10, y0 + 8, layout.data_width - 12, layout.data_height - 10),
        (x0 - 10, y0 - 8, layout.data_width + 20, layout.data_height + 16),
    ]

    variants: list[tuple[np.ndarray, np.ndarray]] = []
    seen = set()
    for rx, ry, rw, rh in rects:
        if rx < 0 or ry < 0 or rx + rw > code.shape[1] or ry + rh > code.shape[0]:
            continue
        key = (rx, ry, rw, rh)
        if key in seen:
            continue
        seen.add(key)
        variants.append(_cell_bits_from_color_rect(code, layout, x=rx, y=ry, width=rw, height=rh))
    return variants


def _cell_bits_from_color(code: np.ndarray, layout: FrameLayout = COLOR_LAYOUT) -> tuple[np.ndarray, np.ndarray]:
    return _cell_bits_variants_from_color(code, layout)[0]


def _cell_bits_variants_from_binary(binary: np.ndarray, layout: FrameLayout) -> list[tuple[np.ndarray, np.ndarray]]:
    x0 = (layout.code_width - layout.data_width) // 2
    y0 = (layout.code_height - layout.data_height) // 2
    rects = [
        (x0, y0, layout.data_width, layout.data_height),
        (x0 - 4, y0 - 2, layout.data_width + 12, layout.data_height + 6),
        (x0 - 6, y0 - 4, layout.data_width + 16, layout.data_height + 8),
        (x0 + 3, y0 - 1, layout.data_width + 8, layout.data_height + 4),
    ]

    variants: list[tuple[np.ndarray, np.ndarray]] = []
    for rx, ry, rw, rh in rects:
        if rx < 0 or ry < 0 or rx + rw > binary.shape[1] or ry + rh > binary.shape[0]:
            continue
        data_region = binary[ry : ry + rh, rx : rx + rw]
        if data_region.shape != (layout.data_height, layout.data_width):
            data_region = cv2.resize(
                data_region,
                (layout.data_width, layout.data_height),
                interpolation=cv2.INTER_AREA,
            )
        cells = data_region.reshape(layout.grid_rows, layout.cell_size, layout.grid_cols, layout.cell_size).mean(axis=(1, 3))
        bits = (cells < 127).astype(np.uint8).reshape(-1)
        variants.append((bits, cells))

    # Phone recordings often leave the outer frame roughly correct while the data
    # grid itself is still sheared. Try a few conservative perspective tweaks.
    destination = np.array(
        [
            [0, 0],
            [layout.data_width - 1, 0],
            [layout.data_width - 1, layout.data_height - 1],
            [0, layout.data_height - 1],
        ],
        dtype=np.float32,
    )
    quads = [
        ((0, 0), (0, 0), (-32, -6), (6, -6)),
        ((10, -2), (4, -2), (-34, -6), (6, -6)),
        ((-6, 0), (12, 0), (-20, -10), (12, -10)),
    ]
    for tl_delta, tr_delta, br_delta, bl_delta in quads:
        source = np.array(
            [
                [x0 + tl_delta[0], y0 + tl_delta[1]],
                [x0 + layout.data_width + tr_delta[0], y0 + tr_delta[1]],
                [x0 + layout.data_width + br_delta[0], y0 + layout.data_height + br_delta[1]],
                [x0 + bl_delta[0], y0 + layout.data_height + bl_delta[1]],
            ],
            dtype=np.float32,
        )
        if (
            np.any(source[:, 0] < 0)
            or np.any(source[:, 1] < 0)
            or np.any(source[:, 0] >= binary.shape[1])
            or np.any(source[:, 1] >= binary.shape[0])
        ):
            continue
        data_region = cv2.warpPerspective(
            binary,
            cv2.getPerspectiveTransform(source, destination),
            (layout.data_width, layout.data_height),
            borderValue=255,
        )
        cells = data_region.reshape(layout.grid_rows, layout.cell_size, layout.grid_cols, layout.cell_size).mean(axis=(1, 3))
        bits = (cells < 127).astype(np.uint8).reshape(-1)
        variants.append((bits, cells))
    return variants


def _binary_variants(square: np.ndarray) -> list[np.ndarray]:
    gray = cv2.cvtColor(square, cv2.COLOR_BGR2GRAY)

    variants: list[np.ndarray] = []

    _, otsu = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    variants.append(otsu)

    clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8)).apply(gray)
    _, clahe_otsu = cv2.threshold(clahe, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    variants.append(clahe_otsu)

    background = cv2.GaussianBlur(gray, (0, 0), sigmaX=31, sigmaY=31)
    normalized = cv2.divide(gray, background, scale=255)
    normalized = cv2.normalize(normalized, None, 0, 255, cv2.NORM_MINMAX)
    _, normalized_otsu = cv2.threshold(normalized, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    variants.append(normalized_otsu)

    adaptive = cv2.adaptiveThreshold(
        clahe,
        255,
        cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
        cv2.THRESH_BINARY,
        31,
        7,
    )
    variants.append(adaptive)

    combined = cv2.bitwise_and(clahe_otsu, adaptive)
    variants.append(combined)

    unique_variants: list[np.ndarray] = []
    seen = set()
    for variant in variants:
        key = variant.tobytes()
        if key in seen:
            continue
        seen.add(key)
        unique_variants.append(variant)
    return unique_variants


def vote_bit_candidates_from_frame(frame: np.ndarray) -> list[tuple[FrameLayout, np.ndarray]]:
    candidates: list[tuple[FrameLayout, np.ndarray]] = []
    try:
        code_candidates = _extract_code_candidates(frame)
    except ValueError:
        return candidates

    for layout, code in code_candidates[:3]:
        variants = _binary_variants(code)
        if not variants:
            continue
        for bits, _cells in _cell_bits_variants_from_binary(variants[-1], layout):
            candidates.append((layout, bits))
    return candidates


def inspect_square(square: np.ndarray, layout: FrameLayout = LAYOUT_V1) -> FrameInspection:
    best_failure: FrameInspection | None = None
    best_failure_score = float("-inf")

    for binary in _binary_variants(square):
        for bits, cells in [_cell_bits_from_binary(binary, layout)]:
            confidence = float(np.abs(cells - 127).mean())

            try:
                packet_bytes = _bits_to_packet(bits, layout)
            except ValueError as exc:
                message = str(exc)
                status: Literal["frame-crc", "packet-invalid"] = "frame-crc" if "CRC" in message else "packet-invalid"
                score = confidence + (1000.0 if status == "frame-crc" else 0.0)
                if score > best_failure_score:
                    best_failure_score = score
                    best_failure = FrameInspection(
                        packet=None,
                        status=status,
                        square=square,
                        binary=binary,
                        bits=bits.copy(),
                        layout=layout,
                    )
                continue

            try:
                return FrameInspection(
                    packet=packet_from_bytes(packet_bytes),
                    status="ok",
                    square=square,
                    binary=binary,
                    bits=bits.copy(),
                    layout=layout,
                )
            except ValueError:
                if confidence > best_failure_score:
                    best_failure_score = confidence
                    best_failure = FrameInspection(
                        packet=None,
                        status="packet-invalid",
                        square=square,
                        binary=binary,
                        bits=bits.copy(),
                        layout=layout,
                    )

    if best_failure is not None:
        return best_failure
    return FrameInspection(packet=None, status="packet-invalid", square=square)


def inspect_color_square(
    square: np.ndarray,
    layout: FrameLayout = COLOR_LAYOUT,
    *,
    use_variants: bool = True,
) -> FrameInspection:
    best_failure: FrameInspection | None = None
    best_failure_rank = {"packet-invalid": 1, "frame-crc": 2}

    variants = _cell_bits_variants_from_color(square, layout)
    if not use_variants:
        variants = variants[:1]

    for bits, _cells in variants:
        try:
            packet_bytes = _bits_to_packet(bits, layout)
        except ValueError as exc:
            message = str(exc)
            status: Literal["frame-crc", "packet-invalid"] = "frame-crc" if "CRC" in message else "packet-invalid"
            failure = FrameInspection(packet=None, status=status, square=square, bits=bits.copy(), layout=layout)
            if best_failure is None or best_failure_rank[status] > best_failure_rank[best_failure.status]:
                best_failure = failure
            continue

        try:
            return FrameInspection(packet=packet_from_bytes(packet_bytes), status="ok", square=square, bits=bits.copy(), layout=layout)
        except ValueError:
            failure = FrameInspection(packet=None, status="packet-invalid", square=square, bits=bits.copy(), layout=layout)
            if best_failure is None:
                best_failure = failure

    return best_failure if best_failure is not None else FrameInspection(packet=None, status="packet-invalid", square=square, layout=layout)


def inspect_frame(frame: np.ndarray) -> FrameInspection:
    centered_candidates = _centered_code_candidates(frame)
    best_failure: FrameInspection | None = None
    best_failure_rank = {"no-quad": 0, "packet-invalid": 1, "frame-crc": 2, "ok": 3}

    def should_replace_best(candidate: FrameInspection) -> bool:
        if best_failure is None:
            return True
        candidate_rank = best_failure_rank[candidate.status]
        current_rank = best_failure_rank[best_failure.status]
        if candidate_rank != current_rank:
            return candidate_rank > current_rank
        candidate_color_depth = candidate.layout.bits_per_cell if candidate.layout is not None else 0
        current_color_depth = best_failure.layout.bits_per_cell if best_failure.layout is not None else 0
        if candidate.status == "frame-crc" and candidate_color_depth != current_color_depth:
            return candidate_color_depth > current_color_depth
        return best_failure.binary is None and candidate.binary is not None

    for layout, code in centered_candidates:
        inspection = (
            inspect_color_square(code, layout, use_variants=False)
            if layout.bits_per_cell == 2
            else inspect_square(code, layout)
        )
        if inspection.status == "ok":
            return inspection
        if should_replace_best(inspection):
            best_failure = inspection

    try:
        candidates = _extract_code_candidates(frame)
    except ValueError:
        if best_failure is None:
            return FrameInspection(packet=None, status="no-quad")
        return best_failure

    for layout, code in candidates:
        inspection = inspect_color_square(code, layout) if layout.bits_per_cell == 2 else inspect_square(code, layout)
        if inspection.status == "ok":
            return inspection
        if should_replace_best(inspection):
            best_failure = inspection
    return best_failure if best_failure is not None else FrameInspection(packet=None, status="no-quad")


def decode_frame_with_status(frame: np.ndarray) -> tuple[Packet | None, Literal["ok", "no-quad", "frame-crc", "packet-invalid"]]:
    inspection = inspect_frame(frame)
    return inspection.packet, inspection.status


def decode_frame(frame: np.ndarray) -> Packet | None:
    packet, status = decode_frame_with_status(frame)
    return packet if status == "ok" else None
