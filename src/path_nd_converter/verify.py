"""Independent-of-the-writer check of a conversion.

For every source tile, rebuild the tile's entropy-coded bytes FROM THE NDPI FILE (restart intervals decoded
back to blocks, regrouped into the tile's MCUs, re-encoded standalone) and compare with the original bytes.
Also compares the decoded coefficient blocks MCU by MCU.

Tiles touching the right/bottom edge are only partly retained (the converter pads the image to a whole number
of MCUs and drops the tile padding beyond that), so for those only the retained MCUs are compared, and the
byte-for-byte comparison is reported for interior tiles only.
"""

from __future__ import annotations

import multiprocessing as mp
import struct
from typing import Any

from .convert import encode_mcus, iter_mcu_rows, make_luts, page_spec, parse_jpeg
from .jpegbits import BitWriter, unstuff


def read_ndpi_page(path: str, k: int) -> dict[str, Any]:
    """Locate page k of an NDPI-style file: strip offset/length, restart-interval starts, dimensions."""
    with open(path, "rb") as fh:
        d = fh.read()
    ifd = struct.unpack("<Q", d[4:12])[0]
    for _ in range(k):
        n = struct.unpack("<H", d[ifd : ifd + 2])[0]
        ifd = struct.unpack("<Q", d[ifd + 2 + 12 * n : ifd + 10 + 12 * n])[0]
    n = struct.unpack("<H", d[ifd : ifd + 2])[0]
    ents, ext = d[ifd + 2 : ifd + 2 + 12 * n], d[ifd + 10 + 12 * n : ifd + 10 + 16 * n]
    t = {}
    for i in range(n):
        tag, _typ, cnt = struct.unpack("<HHI", ents[12 * i : 12 * i + 8])
        t[tag] = (cnt, int.from_bytes(ents[12 * i + 8 : 12 * i + 12] + ext[4 * i : 4 * i + 4], "little"))
    lo = struct.unpack(f"<{t[65426][0]}I", d[t[65426][1] : t[65426][1] + 4 * t[65426][0]])
    hi = struct.unpack(f"<{t[65432][0]}I", d[t[65432][1] : t[65432][1] + 4 * t[65432][0]]) if 65432 in t else None
    starts = [l | ((hi[i] << 32) if hi else 0) for i, l in enumerate(lo)]
    return dict(strip=t[273][1], length=t[279][1], starts=starts, W=t[256][1], H=t[257][1])


def decode_scan(data: bytes, n_mcu: int, spec: dict[str, Any], tabsel: list) -> list:
    _, _, dc_lut, ac_lut = make_luts(spec["dht"])
    data = data + bytes(8)  # padding for the 5-byte read window (see convert.iter_mcu_rows)
    frm = int.from_bytes
    pos, pred, out = 0, [0] * len(spec["comps"]), []
    for _ in range(n_mcu):
        mcu = []
        for ci in spec["bmap"]:
            td, ta = tabsel[ci]
            o = pos & 7
            v = frm(data[pos >> 3 : (pos >> 3) + 5], "big")
            sym, ln = dc_lut[td][(v >> (24 - o)) & 0xFFFF]
            if sym:
                bits = (v >> (40 - o - ln - sym)) & ((1 << sym) - 1)
                pred[ci] += bits if bits >> (sym - 1) else bits - (1 << sym) + 1
            pos += ln + sym
            toks, k, lut = [], 1, ac_lut[ta]
            while k < 64:
                o = pos & 7
                v = frm(data[pos >> 3 : (pos >> 3) + 5], "big")
                rs, ln = lut[(v >> (24 - o)) & 0xFFFF]
                r, s = rs >> 4, rs & 15
                if s == 0:
                    pos += ln
                    toks.append((rs, 0, 0))
                    if r == 15:
                        k += 16
                        continue
                    break
                toks.append((rs, (v >> (40 - o - ln - s)) & ((1 << s) - 1), s))
                pos += ln + s
                k += r + 1
            mcu.append((pred[ci], toks))
        out.append(tuple(mcu))
    return out


def _check_row(args: tuple) -> dict[str, int]:
    src, sp, nd, nk, r = args
    spec, pg = page_spec(src, sp), read_ndpi_page(nd, nk)
    mw, mh = spec["mcu_w"], spec["mcu_h"]
    per_row, rows_total = pg["W"] // mw, pg["H"] // mh
    ipr = len(pg["starts"]) // rows_total
    interval = per_row // ipr
    base, tmr = r * (spec["ch"] // mh), spec["ch"] // mh
    nd_rows = []
    with open(nd, "rb") as ndf, open(src, "rb") as srcf:
        for g in range(base, min(base + tmr, rows_total)):
            row: list = []
            for q in range(ipr):
                idx = g * ipr + q
                a = pg["starts"][idx]
                b = (pg["starts"][idx + 1] - 2) if idx + 1 < len(pg["starts"]) else pg["length"] - 2
                ndf.seek(pg["strip"] + a)
                row += decode_scan(unstuff(ndf.read(b - a)), interval, spec, spec["tabsel"])
            nd_rows.append(row)
        res = dict(tiles=0, interior=0, interior_exact=0, mcus=0, mcus_equal=0, tiles_identical=0)
        for c in range(spec["tcols"]):
            i = r * spec["tcols"] + c
            srcf.seek(spec["offs"][i])
            tile = srcf.read(spec["cnts"][i])
            h = parse_jpeg(tile)
            end = len(tile) - 2 if tile[-2:] == b"\xff\xd9" else len(tile)
            orig = tile[h["scan"] : end]
            src_rows = list(iter_mcu_rows(tile, spec))
            twm = -(-h["sof"]["w"] // mw)
            interior = (c + 1) * twm <= per_row and base + len(src_rows) <= rows_total
            rebuilt, ok = [], True
            for j, srow in enumerate(src_rows):
                for x in range(twm):
                    gx = c * (spec["cw"] // mw) + x
                    if base + j < rows_total and gx < per_row:
                        res["mcus"] += 1
                        nm = nd_rows[j][gx]
                        if nm == srow[x]:
                            res["mcus_equal"] += 1
                        else:
                            ok = False
                        if interior:
                            rebuilt.append(nm)
            res["tiles"] += 1
            res["tiles_identical"] += ok
            if interior:
                res["interior"] += 1
                bw = BitWriter()
                encode_mcus(rebuilt, spec, bw, spec["tabsel"])
                bw.flush_to_byte()
                res["interior_exact"] += bytes(bw.buf) == orig
    return res


def verify_page(src: str, nd: str, src_page: int, ndpi_page: int, max_tile_rows: int | None = None, workers: int | None = None) -> dict[str, int]:
    """Verify one converted page. Returns counts; a perfect conversion has mcus_equal == mcus,
    tiles_identical == tiles and interior_exact == interior."""
    spec = page_spec(src, src_page)
    rows = list(range(spec["trows"]))
    if max_tile_rows and max_tile_rows < len(rows):
        step = len(rows) / max_tile_rows
        rows = sorted({int(i * step) for i in range(max_tile_rows)} | {0, len(rows) - 1})
    tot = dict(tiles=0, interior=0, interior_exact=0, mcus=0, mcus_equal=0, tiles_identical=0)
    with mp.Pool(workers or max(1, mp.cpu_count() - 1)) as pool:
        for res in pool.imap_unordered(_check_row, [(src, src_page, nd, ndpi_page, r) for r in rows]):
            for k in tot:
                tot[k] += res[k]
    tot["tile_rows_checked"], tot["tile_rows_total"] = len(rows), spec["trows"]
    return tot


def is_perfect(r: dict[str, int]) -> bool:
    return r["mcus"] > 0 and r["mcus_equal"] == r["mcus"] and r["tiles_identical"] == r["tiles"] and r["interior_exact"] == r["interior"]
