"""SVS (baseline JPEG tiles/strips) -> NDPI-style TIFF.

Lossless at the entropy-coding level. Each block's Huffman *symbols* and raw extra bits are moved
verbatim; only DC differences are re-encoded, because restart markers reset DC prediction. No pixel is
decoded or re-quantised. See docs/FINDINGS.md for what the NDPI layout looks like and what was verified.

EXPERIMENTAL. Output has not been opened in Hamamatsu software.
"""

from __future__ import annotations

import multiprocessing as mp
import os
import re
import struct
import time
from datetime import datetime, timezone
from typing import Any

import tifffile

from . import __version__
from .jpegbits import BitWriter, Unsupported, build_codes, build_lookup, segment, unstuff

# JPEG's 16-bit size fields can't express more than this; NDPI writers store 0 for larger dimensions.
JPEG_MAX_DIM = 65500


# --------------------------------------------------------------------------- JPEG parsing
def parse_jpeg(buf: bytes) -> dict[str, Any]:
    """Parse markers up to the first scan. Works on full streams and on table-only (JPEGTables) blobs."""
    i = 2 if buf[:2] == b"\xff\xd8" else 0
    out: dict[str, Any] = dict(dqt=[], dht=[], sof=None, sos=None, dri=0, scan=None)
    while i + 4 <= len(buf) and buf[i] == 0xFF:
        m = buf[i + 1]
        if m == 0xD9:
            break
        if m in (0xD8, 0x01) or 0xD0 <= m <= 0xD7:
            i += 2
            continue
        ln = struct.unpack(">H", buf[i + 2 : i + 4])[0]
        body, raw = buf[i + 4 : i + 2 + ln], buf[i : i + 2 + ln]
        if m == 0xDB:
            out["dqt"].append(bytes(raw))
        elif m == 0xC4:
            j = 0
            while j < len(body):
                n = sum(body[j + 1 : j + 17])
                out["dht"].append((body[j], list(body[j + 1 : j + 17]), list(body[j + 17 : j + 17 + n]), bytes(body[j : j + 17 + n])))
                j += 17 + n
        elif m in (0xC0, 0xC1):
            h, w = struct.unpack(">HH", body[1:5])
            out["sof"] = dict(
                h=h, w=w, prec=body[0],
                comps=[(body[6 + 3 * k], body[7 + 3 * k] >> 4, body[7 + 3 * k] & 15, body[8 + 3 * k]) for k in range(body[5])],
            )
        elif m in (0xC2, 0xC3) or (0xC5 <= m <= 0xCF and m not in (0xC8, 0xCC)):
            raise Unsupported(f"JPEG process marker 0x{m:02X}: only baseline/extended sequential Huffman is handled")
        elif m == 0xDD:
            out["dri"] = struct.unpack(">H", body[:2])[0]
        elif m == 0xDA:
            out["sos"] = [(body[1 + 2 * k], body[2 + 2 * k] >> 4, body[2 + 2 * k] & 15) for k in range(body[0])]
            out["scan"] = i + 2 + ln
            break
        i += 2 + ln
    return out


_LUT_CACHE: dict[bytes, tuple] = {}


def make_luts(dht: list) -> tuple:
    """(dc_enc, ac_enc, dc_lut, ac_lut), cached by table bytes."""
    key = b"".join(d[3] for d in dht)
    if key not in _LUT_CACHE:
        dc_enc = {d[0] & 15: build_codes(d[1], d[2]) for d in dht if d[0] >> 4 == 0}
        ac_enc = {d[0] & 15: build_codes(d[1], d[2]) for d in dht if d[0] >> 4 == 1}
        _LUT_CACHE[key] = (dc_enc, ac_enc, {k: build_lookup(v) for k, v in dc_enc.items()}, {k: build_lookup(v) for k, v in ac_enc.items()})
    return _LUT_CACHE[key]


# --------------------------------------------------------------------------- page spec
def page_spec(src: str, pi: int) -> dict[str, Any]:
    """Everything the converter needs to know about one JPEG page. Raises Unsupported with a reason."""
    with tifffile.TiffFile(src) as t:
        p = t.pages[pi]
        if int(p.compression) != 7:
            raise Unsupported(f"TIFF compression {int(p.compression)} ({p.compression.name}); NDPI can only hold JPEG")
        w0, h0, tiled = p.imagewidth, p.imagelength, bool(p.is_tiled)
        cw, ch = (p.tilewidth, p.tilelength) if tiled else (w0, p.rowsperstrip)
        spec: dict[str, Any] = dict(
            pi=pi, W0=w0, H0=h0, cw=cw, ch=ch, tiled=tiled, tcols=-(-w0 // cw), trows=-(-h0 // ch),
            offs=[int(x) for x in p.dataoffsets], cnts=[int(x) for x in p.databytecounts],
            jt=bytes(p.tags[347].value) if 347 in p.tags else b"",
            rgb=p.photometric.name == "RGB",
            desc=p.tags[270].value if 270 in p.tags else "",
        )
    if not spec["offs"] or min(spec["cnts"]) == 0:
        raise Unsupported("page has missing or zero-length tiles/strips")
    with open(src, "rb") as fh:
        fh.seek(spec["offs"][0])
        t0 = fh.read(spec["cnts"][0])
    h = parse_jpeg(t0)
    tabs = parse_jpeg(spec["jt"]) if spec["jt"] else h
    sof = h["sof"]
    if sof is None or h["sos"] is None:
        raise Unsupported("could not find a baseline JPEG frame in the first tile")
    if sof["prec"] != 8:
        raise Unsupported(f"{sof['prec']}-bit JPEG samples")
    if not tabs["dqt"] or not tabs["dht"]:
        raise Unsupported("no quantisation/Huffman tables found (neither JPEGTables nor embedded in the first tile)")
    spec.update(comps=sof["comps"], sos=h["sos"], dqt=tabs["dqt"], dht=tabs["dht"])
    maxh, maxv = max(c[1] for c in sof["comps"]), max(c[2] for c in sof["comps"])
    spec["mcu_w"], spec["mcu_h"] = 8 * maxh, 8 * maxv
    spec["bmap"] = [ci for ci, c in enumerate(sof["comps"]) for _ in range(c[1] * c[2])]
    if ch % spec["mcu_h"] or (tiled and cw % spec["mcu_w"]):
        raise Unsupported(f"chunk size {cw}x{ch} is not a multiple of the {spec['mcu_w']}x{spec['mcu_h']} MCU")
    sel = {cid: (td, ta) for cid, td, ta in spec["sos"]}
    spec["tabsel"] = [sel[c[0]] for c in sof["comps"]]
    return spec


def sampling_label(comps: list) -> str:
    return {(1, 1): "4:4:4", (2, 1): "4:2:2", (1, 2): "4:4:0", (2, 2): "4:2:0", (4, 1): "4:1:1"}.get((comps[0][1], comps[0][2]), f"{comps[0][1]}x{comps[0][2]}")


def inspect_file(src: str) -> list[dict[str, Any]]:
    """One row per TIFF page: what it is and whether this converter can carry it."""
    rows = []
    with tifffile.TiffFile(src) as t:
        n = len(t.pages)
        meta = [(i, t.pages[i].imagewidth, t.pages[i].imagelength, t.pages[i].compression.name, bool(t.pages[i].is_tiled)) for i in range(n)]
    for i, w, h, comp, tiled in meta:
        row: dict[str, Any] = dict(page=i, width=w, height=h, compression=comp, tiled=tiled, convertible=False, note="")
        try:
            s = page_spec(src, i)
            row.update(convertible=True, sampling=sampling_label(s["comps"]), tables="shared (JPEGTables)" if s["jt"] else "embedded per tile", note="")
        except Unsupported as exc:
            row["note"] = str(exc)
        rows.append(row)
    return rows


# --------------------------------------------------------------------------- decode / encode
def iter_mcu_rows(tile: bytes, spec: dict[str, Any]):
    """Yield one MCU row at a time. An MCU is a tuple of blocks; a block is (dc_abs, [(rs, extra_bits, nbits)])."""
    hdr = parse_jpeg(tile)
    if hdr["dri"]:
        raise Unsupported("source tile already has restart markers")
    sof = hdr["sof"]
    if sof is None or hdr["sos"] is None:
        raise Unsupported("tile has no baseline JPEG frame")
    if sof["comps"] != spec["comps"]:
        raise Unsupported("tile sampling/component layout differs within a page")
    if hdr["dqt"] and b"".join(hdr["dqt"]) != b"".join(spec["dqt"]):
        raise Unsupported("tile quantisation tables differ within a page (pass-through would change pixels)")
    _, _, dc_lut, ac_lut = make_luts(hdr["dht"] if hdr["dht"] else spec["dht"])
    sel = {cid: (td, ta) for cid, td, ta in hdr["sos"]}
    tabsel = [sel[c[0]] for c in sof["comps"]]
    end = len(tile) - 2 if tile[-2:] == b"\xff\xd9" else len(tile)
    data = unstuff(tile[hdr["scan"] : end])
    big = int.from_bytes(data + b"\x00\x00\x00", "big")
    total = (len(data) + 3) * 8
    mcus_w, mcus_h = -(-sof["w"] // spec["mcu_w"]), -(-sof["h"] // spec["mcu_h"])
    bmap, pos, pred = spec["bmap"], 0, [0] * len(spec["comps"])
    for _ in range(mcus_h):
        row = []
        for _ in range(mcus_w):
            mcu = []
            for ci in bmap:
                td, ta = tabsel[ci]
                sym, ln = dc_lut[td][(big >> (total - pos - 16)) & 0xFFFF]
                pos += ln
                if sym:
                    bits = (big >> (total - pos - sym)) & ((1 << sym) - 1)
                    pos += sym
                    pred[ci] += bits if bits >> (sym - 1) else bits - (1 << sym) + 1
                toks, k, lut = [], 1, ac_lut[ta]
                while k < 64:
                    rs, ln = lut[(big >> (total - pos - 16)) & 0xFFFF]
                    pos += ln
                    r, s = rs >> 4, rs & 15
                    if s == 0:
                        toks.append((rs, 0, 0))
                        if r == 15:
                            k += 16
                            continue
                        break
                    bits = (big >> (total - pos - s)) & ((1 << s) - 1)
                    pos += s
                    toks.append((rs, bits, s))
                    k += r + 1
                mcu.append((pred[ci], toks))
            row.append(tuple(mcu))
        yield row


def encode_mcus(mcus, spec: dict[str, Any], bw: BitWriter, out_sel: list) -> None:
    """Encode MCUs as one entropy segment; DC prediction starts from zero."""
    dc_enc, ac_enc, _, _ = make_luts(spec["dht"])
    pred = [0] * len(spec["comps"])
    bmap = spec["bmap"]
    for mcu in mcus:
        for bi, (dc, toks) in enumerate(mcu):
            ci = bmap[bi]
            td, ta = out_sel[ci]
            d = dc - pred[ci]
            pred[ci] = dc
            cat = abs(d).bit_length()
            code, ln = dc_enc[td][cat]
            bw.put(code, ln)
            if cat:
                bw.put(d if d > 0 else d + (1 << cat) - 1, cat)
            aenc = ac_enc[ta]
            for rs, bits, s in toks:
                if rs not in aenc:
                    raise Unsupported(f"output Huffman table has no code for symbol 0x{rs:02X}")
                code, ln = aenc[rs]
                bw.put(code, ln)
                if s:
                    bw.put(bits, s)


def encode_tile_row(args: tuple) -> list[bytes]:
    """Worker: re-encode one row of source tiles as restart intervals (top level so it can be pickled)."""
    src, pi, r, interval, per_row, rows_total = args
    spec = page_spec(src, pi)
    gens, widths = [], []
    with open(src, "rb") as fh:
        for c in range(spec["tcols"]):
            i = r * spec["tcols"] + c
            fh.seek(spec["offs"][i])
            tile = fh.read(spec["cnts"][i])
            gens.append(iter_mcu_rows(tile, spec))
            widths.append(-(-parse_jpeg(tile)["sof"]["w"] // spec["mcu_w"]))
    base, out, bw = r * (spec["ch"] // spec["mcu_h"]), [], BitWriter()
    first_w = widths[0]
    while base < rows_total:
        rows = [next(g, None) for g in gens]
        if rows[0] is None:
            break
        row: list = []
        for c, rr in enumerate(rows):
            row += rr[: min(widths[c], per_row - c * first_w)]
        base += 1
        for a in range(0, per_row, interval):
            encode_mcus(row[a : a + interval], spec, bw, spec["tabsel"])
            bw.flush_to_byte()
            out.append(bw.take())
    return out


# --------------------------------------------------------------------------- writer
def build_header(spec: dict[str, Any], wp: int, hp: int, interval: int) -> bytes:
    """JPEG header for a whole level. Component ids R,G,B make libjpeg-based decoders treat Aperio's
    RGB-in-JPEG data as RGB; other decoders may not (see README, 'Known problems')."""
    ids = list(b"RGB") if spec["rgb"] and len(spec["comps"]) == 3 else [c[0] for c in spec["comps"]]
    sofw = 0 if wp > JPEG_MAX_DIM else wp  # scanners write 0 for sizes JPEG cannot express
    sofh = 0 if hp > JPEG_MAX_DIM else hp
    h = b"\xff\xd8" + b"".join(spec["dqt"]) + b"".join(b"\xff\xc4" + struct.pack(">H", len(d[3]) + 2) + d[3] for d in spec["dht"])
    comps = b"".join(bytes([ids[k], (spec["comps"][k][1] << 4) | spec["comps"][k][2], spec["comps"][k][3]]) for k in range(len(ids)))
    h += segment(0xC0, bytes([8]) + struct.pack(">HH", sofh, sofw) + bytes([len(ids)]) + comps)
    h += segment(0xDD, struct.pack(">H", interval))
    sel = b"".join(bytes([ids[k], (spec["tabsel"][k][0] << 4) | spec["tabsel"][k][1]]) for k in range(len(ids)))
    h += segment(0xDA, bytes([len(ids)]) + sel + bytes([0, 63, 0]))
    return h


class _Out:
    def __init__(self, path: str) -> None:
        self.f = open(path, "wb")
        self.f.write(b"II*\x00" + bytes(8))  # classic-TIFF magic + 64-bit first-IFD offset, patched at the end

    def blob(self, data: bytes) -> int:
        if self.f.tell() % 2:
            self.f.write(b"\x00")
        off = self.f.tell()
        self.f.write(data)
        return off


def convert_page(src: str, pi: int, pool, out: _Out, repeat: int = 1) -> dict[str, Any]:
    spec = page_spec(src, pi)
    mw, mh = spec["mcu_w"], spec["mcu_h"]
    wp, hp = -(-spec["W0"] // mw) * mw, -(-spec["H0"] // mh) * mh
    per_row, rows_total = wp // mw, hp // mh
    interval = max([d for d in range(16, min(256, per_row) + 1) if per_row % d == 0] or [per_row])
    intervals: list[bytes] = []
    for ivs in pool.imap(encode_tile_row, [(src, pi, r, interval, per_row, rows_total) for r in range(spec["trows"])]):
        intervals += ivs
    if len(intervals) != rows_total * (per_row // interval):
        raise RuntimeError(f"internal error: {len(intervals)} intervals, expected {rows_total * (per_row // interval)}")
    header = build_header(spec, wp, hp * repeat, interval)
    f = out.f
    if f.tell() % 2:
        f.write(b"\x00")
    strip_off = f.tell()
    f.write(header)
    starts, rst, pos, n, n_total = [len(header)], 0, len(header), 0, len(intervals) * repeat
    for _ in range(repeat):
        for b in intervals:
            f.write(b)
            pos += len(b)
            n += 1
            if n < n_total:
                f.write(bytes([0xFF, 0xD0 + rst]))
                rst = (rst + 1) % 8
                pos += 2
                starts.append(pos)
    f.write(b"\xff\xd9")
    return dict(spec=spec, W=wp, H=hp * repeat, interval=interval, strip_off=strip_off, strip_len=pos + 2, starts=starts)


def _ascii(out: _Out, e: dict, tag: int, text: str) -> None:
    data = text.encode("ascii", "replace") + b"\x00"
    off = out.blob(data)
    e[tag] = (2, len(data), struct.pack("<I", off & 0xFFFFFFFF), struct.pack("<I", off >> 32))


def write_ifds(out: _Out, pages: list[dict], mpp0: float, mag0: float, props: dict[str, Any]) -> None:
    """Directories last. Real NDPI layout, verified against scanner files:
    entries | next-IFD offset (8 bytes) | value extensions (4 bytes per entry = high 32 bits of each value/offset)."""
    f = out.f
    entries = []
    w0 = pages[0]["W"]
    when = time.strftime("%Y:%m:%d %H:%M:%S")
    for pg in pages:
        macro = pg.get("macro", False)
        e: dict[int, tuple] = {}

        def inline(tag: int, typ: int, raw: bytes, cnt: int = 1) -> None:
            e[tag] = (typ, cnt, raw, bytes(4))

        def num(tag: int, typ: int, v: int) -> None:
            e[tag] = (typ, 1, struct.pack("<I", v & 0xFFFFFFFF), struct.pack("<I", v >> 32))

        def ptr(tag: int, typ: int, cnt: int, data: bytes) -> None:
            off = out.blob(data)
            e[tag] = (typ, cnt, struct.pack("<I", off & 0xFFFFFFFF), struct.pack("<I", off >> 32))

        short = lambda v: struct.pack("<HH", v, 0)
        num(256, 4, pg["W"])
        num(257, 4, pg["H"])
        ptr(258, 3, 3, struct.pack("<3H", 8, 8, 8))
        inline(259, 3, short(7))
        inline(262, 3, short(6))
        _ascii(out, e, 271, "path-nd-converter")
        _ascii(out, e, 272, "svs2ndpi (experimental)")
        num(273, 4, pg["strip_off"])
        inline(277, 3, short(3))
        num(278, 4, pg["H"])
        num(279, 4, pg["strip_len"])
        ds = w0 / pg["W"]
        if not macro:
            res = int(round(10000.0 / (mpp0 * ds)))
            ptr(282, 5, 1, struct.pack("<II", res, 1))
            ptr(283, 5, 1, struct.pack("<II", res, 1))
            inline(296, 3, short(3))
        _ascii(out, e, 305, f"path-nd-converter {__version__}")
        _ascii(out, e, 306, when)  # when this file was written, not when the slide was scanned
        sub = pg["spec"]["comps"][0]
        inline(530, 3, struct.pack("<HH", sub[1], sub[2]), cnt=2)
        ptr(532, 4, 6, struct.pack("<6I", 0, 255, 128, 255, 128, 255))
        inline(65420, 3, short(1))
        inline(65421, 11, struct.pack("<f", -1.0 if macro else mag0 / ds))
        for t in (65422, 65423, 65424):  # slide-centre offsets and focal plane: unknown for a converted file
            inline(t, 9, struct.pack("<i", 0))
        starts = pg["starts"]
        ptr(65426, 4, len(starts), struct.pack(f"<{len(starts)}I", *[s & 0xFFFFFFFF for s in starts]))
        if max(starts) >= 2**32:  # strips over 4 GiB need the high halves too
            ptr(65432, 4, len(starts), struct.pack(f"<{len(starts)}I", *[s >> 32 for s in starts]))
        if pg is pages[0] and props:
            txt = ("\r\n".join(f"{k}={v}" for k, v in props.items()) + "\r\n").encode("ascii", "replace") + b"\x00"
            ptr(65449, 2, len(txt), txt)
        entries.append(e)
    if f.tell() % 2:
        f.write(b"\x00")
    offs, pos = [], f.tell()
    for e in entries:
        offs.append(pos)
        pos += 2 + 16 * len(e) + 8
    for k, e in enumerate(entries):
        f.write(struct.pack("<H", len(e)))
        for tag in sorted(e):
            typ, cnt, lo, _hi = e[tag]
            f.write(struct.pack("<HHI", tag, typ, cnt) + lo)
        f.write(struct.pack("<Q", offs[k + 1] if k + 1 < len(entries) else 0))
        for tag in sorted(e):
            f.write(e[tag][3])
    f.seek(4)
    f.write(struct.pack("<Q", offs[0]))
    f.close()


def convert(src: str, dst: str, levels: list[int], macro: int | None = None, repeat: int = 1, workers: int | None = None, log=print) -> None:
    """Convert the given JPEG pages of an SVS (largest level first) to a multi-level NDPI-style file.

    `repeat` stacks the first level K times and exists only to build >4 GiB test files."""
    if not levels:
        raise ValueError("at least one level page is required")
    s0 = page_spec(src, levels[0])
    mpp = re.search(r"MPP\s*=\s*([0-9.]+)", s0["desc"])
    mag = re.search(r"AppMag\s*=\s*([0-9.]+)", s0["desc"])
    mpp0, mag0 = float(mpp.group(1)) if mpp else 0.5, float(mag.group(1)) if mag else 20.0
    props = {
        "Provenance.Notice": "Experimental conversion; this file was NOT produced by a Hamamatsu scanner",
        "Provenance.SourceFormat": "Aperio SVS (TIFF/JPEG)",
        "Provenance.SourceWidth": s0["W0"],
        "Provenance.SourceHeight": s0["H0"],
        "Provenance.Converter": f"path-nd-converter {__version__} (lossless entropy-layer transcode)",
        "Provenance.ConvertedAtUTC": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "Provenance.SourceDescription": s0["desc"].replace("\r", " ").replace("\n", " | ")[:900],
    }
    out = _Out(dst)
    pages = []
    try:
        with mp.Pool(workers or max(1, mp.cpu_count() - 1)) as pool:
            for k, pi in enumerate(levels):
                pg = convert_page(src, pi, pool, out, repeat=repeat if k == 0 else 1)
                log(f"page {pi}: {pg['spec']['W0']}x{pg['spec']['H0']} -> {pg['W']}x{pg['H']}, {sampling_label(pg['spec']['comps'])}, "
                    f"{pg['spec']['mcu_w']}x{pg['spec']['mcu_h']} MCU, restart interval {pg['interval']}, {pg['strip_len'] / 1e6:.1f} MB")
                pages.append(pg)
            if macro is not None:
                pg = convert_page(src, macro, pool, out)
                pg["macro"] = True
                log(f"macro page {macro}: {pg['W']}x{pg['H']}")
                pages.append(pg)
        write_ifds(out, pages, mpp0, mag0, props)
    except BaseException:
        out.f.close()
        if os.path.exists(dst):
            os.remove(dst)  # never leave a truncated file that looks like a result
        raise
