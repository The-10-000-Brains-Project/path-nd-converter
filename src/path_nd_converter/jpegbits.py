"""Bit-level JPEG helpers: canonical Huffman tables, byte (un)stuffing, a bit writer."""

from __future__ import annotations

import re
import struct

_RST = re.compile(rb"\xff[\xd0-\xd7]")


class Unsupported(Exception):
    """The input is valid but outside what this converter can carry over losslessly."""


def build_codes(counts: list[int], symbols: list[int]) -> dict[int, tuple[int, int]]:
    """Canonical Huffman table -> {symbol: (code, length)}."""
    enc: dict[int, tuple[int, int]] = {}
    code = k = 0
    for length in range(1, 17):
        for _ in range(counts[length - 1]):
            enc[symbols[k]] = (code, length)
            code += 1
            k += 1
        code <<= 1
    return enc


def build_lookup(enc: dict[int, tuple[int, int]]) -> list:
    """16-bit prefix table -> (symbol, length)."""
    table: list = [None] * 65536
    for sym, (code, length) in enc.items():
        base = code << (16 - length)
        for x in range(1 << (16 - length)):
            table[base + x] = (sym, length)
    return table


def unstuff(scan: bytes) -> bytes:
    """Remove the 0x00 stuffed after every 0xFF in entropy-coded data."""
    if _RST.search(scan):
        raise Unsupported("entropy-coded data already contains restart markers")
    return scan.replace(b"\xff\x00", b"\xff")


class BitWriter:
    """Accumulates Huffman output with JPEG byte stuffing."""

    def __init__(self) -> None:
        self.buf = bytearray()
        self.acc = 0
        self.n = 0

    def put(self, code: int, length: int) -> None:
        self.acc = (self.acc << length) | code
        self.n += length
        while self.n >= 8:
            self.n -= 8
            b = (self.acc >> self.n) & 0xFF
            self.buf.append(b)
            if b == 0xFF:
                self.buf.append(0)
        self.acc &= (1 << self.n) - 1

    def flush_to_byte(self) -> None:
        """Pad the final partial byte with 1 bits, as the JPEG standard requires."""
        if self.n:
            self.put((1 << (8 - self.n)) - 1, 8 - self.n)

    def take(self) -> bytes:
        out = bytes(self.buf)
        self.buf = bytearray()
        return out


def segment(marker: int, payload: bytes) -> bytes:
    return b"\xff" + bytes([marker]) + struct.pack(">H", len(payload) + 2) + payload
