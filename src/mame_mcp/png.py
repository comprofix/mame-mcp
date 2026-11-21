"""Minimal, dependency-free PNG reader used to validate and summarise the screenshots MAME writes.

`inspect_png` checks the structure (signature, chunk CRCs, IHDR first, IEND last, IDAT data that inflates to exactly the
size the header implies) and reports the dimensions. `decode_pixels` additionally reverses the scanline filters so a
caller can compare images or tell a blank screen from a drawn one. Non-interlaced 8-bit images only (what MAME writes);
anything else is reported as unsupported for pixel decoding rather than guessed at.
"""
from __future__ import annotations

import struct
import zlib
from dataclasses import dataclass
from typing import Any

SIGNATURE = b"\x89PNG\r\n\x1a\n"
MAX_PIXELS = 4_000_000          # refuse to decode anything bigger than this (MAME screens are far smaller)
_CHANNELS = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}


class PngError(ValueError):
    """The bytes are not a valid PNG (message says why)."""


@dataclass
class PngInfo:
    width: int
    height: int
    bit_depth: int
    color_type: int
    interlace: int
    idat: bytes
    palette: bytes | None

    def summary(self) -> dict[str, Any]:
        return {"width": self.width, "height": self.height, "bit_depth": self.bit_depth, "color_type": self.color_type,
                "interlaced": bool(self.interlace)}


def _row_bytes(info: PngInfo) -> int:
    return (info.width * _CHANNELS[info.color_type] * info.bit_depth + 7) // 8


def inspect_png(data: bytes) -> PngInfo:
    if len(data) < 8 or data[:8] != SIGNATURE:
        raise PngError("missing PNG signature")
    pos, first, seen_iend = 8, True, False
    ihdr: tuple[int, ...] | None = None
    idat, palette = [], None
    while pos < len(data):
        if seen_iend:
            raise PngError("data after IEND")
        if pos + 12 > len(data):
            raise PngError("truncated chunk header")
        (length,), ctype = struct.unpack(">I", data[pos:pos + 4]), data[pos + 4:pos + 8]
        end = pos + 8 + length
        if end + 4 > len(data):
            raise PngError(f"truncated {ctype!r} chunk")
        body = data[pos + 8:end]
        (crc,) = struct.unpack(">I", data[end:end + 4])
        if zlib.crc32(ctype + body) & 0xFFFFFFFF != crc:
            raise PngError(f"bad CRC in {ctype!r} chunk")
        if first:
            if ctype != b"IHDR" or length != 13:
                raise PngError("first chunk is not a 13-byte IHDR")
            ihdr = struct.unpack(">IIBBBBB", body)
            first = False
        elif ctype == b"IDAT":
            idat.append(body)
        elif ctype == b"PLTE":
            palette = body
        elif ctype == b"IEND":
            seen_iend = True
        pos = end + 4
    if ihdr is None or not seen_iend:
        raise PngError("no IHDR or no IEND chunk")
    w, h, depth, ctype_, comp, filt, interlace = ihdr
    if w < 1 or h < 1 or comp != 0 or filt != 0 or interlace not in (0, 1) or ctype_ not in _CHANNELS \
            or depth not in (1, 2, 4, 8, 16):
        raise PngError(f"unsupported or invalid IHDR {ihdr}")
    if ctype_ == 3 and palette is None:
        raise PngError("palette image without PLTE")
    if not idat:
        raise PngError("no IDAT data")
    info = PngInfo(w, h, depth, ctype_, interlace, b"".join(idat), palette)
    if not interlace:
        try:
            raw = zlib.decompress(info.idat)
        except zlib.error as e:
            raise PngError(f"IDAT does not inflate: {e}") from e
        if len(raw) != h * (1 + _row_bytes(info)):
            raise PngError(f"IDAT inflates to {len(raw)} bytes, header implies {h * (1 + _row_bytes(info))}")
    return info


def decode_pixels(data: bytes) -> tuple[PngInfo, list[bytes]]:
    """Validate `data` and return (info, scanlines) with the PNG filters reversed. 8-bit non-interlaced only."""
    info = inspect_png(data)
    if info.interlace or info.bit_depth != 8:
        raise PngError("pixel decoding supports only non-interlaced 8-bit images")
    if info.width * info.height > MAX_PIXELS:
        raise PngError("image too large to decode")
    bpp = _CHANNELS[info.color_type]
    stride = _row_bytes(info)
    raw = zlib.decompress(info.idat)
    rows: list[bytes] = []
    prev = bytearray(stride)
    for y in range(info.height):
        base = y * (stride + 1)
        ft = raw[base]
        line = bytearray(raw[base + 1:base + 1 + stride])
        if ft == 1:
            for i in range(bpp, stride):
                line[i] = (line[i] + line[i - bpp]) & 255
        elif ft == 2:
            for i in range(stride):
                line[i] = (line[i] + prev[i]) & 255
        elif ft == 3:
            for i in range(stride):
                left = line[i - bpp] if i >= bpp else 0
                line[i] = (line[i] + ((left + prev[i]) >> 1)) & 255
        elif ft == 4:
            for i in range(stride):
                a = line[i - bpp] if i >= bpp else 0
                b = prev[i]
                c = prev[i - bpp] if i >= bpp else 0
                pa, pb, pc = abs(b - c), abs(a - c), abs(a + b - 2 * c)
                pred = a if pa <= pb and pa <= pc else (b if pb <= pc else c)
                line[i] = (line[i] + pred) & 255
        elif ft != 0:
            raise PngError(f"unknown filter type {ft}")
        rows.append(bytes(line))
        prev = line
    return info, rows


def summarise_pixels(data: bytes) -> dict[str, Any]:
    """Pixel facts for a research reader: how many distinct colours, and whether the image is a single flat colour."""
    info, rows = decode_pixels(data)
    bpp = _CHANNELS[info.color_type]
    colours: set[bytes] = set()
    for row in rows:
        colours.update(row[i:i + bpp] for i in range(0, len(row), bpp))
        if len(colours) > 4096:
            break
    return {"distinct_colours": len(colours) if len(colours) <= 4096 else ">4096", "single_colour": len(colours) == 1,
            "pixel_sha256": __import__("hashlib").sha256(b"".join(rows)).hexdigest()}
