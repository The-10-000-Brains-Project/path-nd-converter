"""Opt-in, NOT lossless: re-encode JPEG 2000 pages as JPEG so they can go into an NDPI.

NDPI holds only JPEG, so JPEG 2000 pixels cannot be carried exactly. The closest valid option is to decode each
tile and encode it as a baseline JPEG that stores RGB directly (no YCbCr conversion, which adds rounding error)
with all-ones quantisation tables (quality 100), 4:4:4, the same way Aperio's own JPEG slides are stored.
The result is written as a temporary JPEG-tiled TIFF that the normal converter then handles. The pixel error
against the decoded JPEG 2000 is measured while encoding and returned.
"""

from __future__ import annotations

import multiprocessing as mp
from typing import Any

import numpy as np
import tifffile

from .jpegbits import Unsupported

# TIFF compression codes for JPEG 2000: Aperio YCbCr, Aperio RGB, standard.
J2K_CODES = {33003, 33005, 34712}


def _imagecodecs():
    try:
        import imagecodecs
    except ImportError as exc:  # pragma: no cover
        raise Unsupported("re-encoding JPEG 2000 needs the 'imagecodecs' package (pip install path-nd-converter[jpeg2000])") from exc
    return imagecodecs


def _reencode_row(args: tuple) -> tuple[list[bytes], list[int]]:
    src, pi, r, quality = args
    ic = _imagecodecs()
    out: list[bytes] = []
    n = exact = maxerr = sumabs = 0
    with tifffile.TiffFile(src) as t, open(src, "rb") as fh:
        p = t.pages[pi]
        th, tw, tcols = p.tilelength, p.tilewidth, -(-p.imagewidth // p.tilewidth)
        vis_h = min(th, p.imagelength - r * th)
        for c in range(tcols):
            i = r * tcols + c
            fh.seek(p.dataoffsets[i])
            seg = np.asarray(p.decode(fh.read(p.databytecounts[i]), i, jpegtables=p.jpegtables)[0])
            seg = seg.reshape(seg.shape[-3:])
            tile = np.zeros((th, tw, 3), np.uint8)
            tile[: seg.shape[0], : seg.shape[1]] = seg[:, :, :3]
            enc = ic.jpeg8_encode(tile, level=quality, colorspace="rgb", outcolorspace="rgb", subsampling="444")
            dec = ic.jpeg8_decode(enc, colorspace="rgb", outcolorspace="rgb")
            vis_w = min(tw, p.imagewidth - c * tw)
            d = np.abs(tile[:vis_h, :vis_w].astype(np.int16) - dec[:vis_h, :vis_w].astype(np.int16))
            n += d.shape[0] * d.shape[1]
            exact += int((d.max(axis=2) == 0).sum())
            maxerr = max(maxerr, int(d.max()))
            sumabs += int(d.sum())
            out.append(bytes(enc))
    return out, [n, exact, maxerr, sumabs]


def reencode_page(src: str, pi: int, dst: str, quality: int = 100, workers: int | None = None) -> dict[str, Any]:
    """Write page `pi` of `src` (JPEG 2000) to `dst` as a single-page JPEG-tiled TIFF.
    Returns measured error against the decoded JPEG 2000: pixels, exact_pixels, max_error, mean_abs_error."""
    with tifffile.TiffFile(src) as t:
        p = t.pages[pi]
        if int(p.compression) not in J2K_CODES:
            raise Unsupported(f"page {pi} is not JPEG 2000")
        if not p.is_tiled or p.samplesperpixel != 3 or p.bitspersample != 8 and p.bitspersample != (8, 8, 8):
            raise Unsupported("only 8-bit RGB tiled JPEG 2000 pages are handled")
        w, h, tw, th = p.imagewidth, p.imagelength, p.tilewidth, p.tilelength
        desc = p.tags[270].value if 270 in p.tags else None
        trows = -(-h // th)
    totals = [0, 0, 0, 0]
    with mp.Pool(workers or max(1, mp.cpu_count() - 1)) as pool:
        stream = pool.imap(_reencode_row, [(src, pi, r, quality) for r in range(trows)])

        def tiles():
            for jpegs, st in stream:
                totals[0] += st[0]
                totals[1] += st[1]
                totals[2] = max(totals[2], st[2])
                totals[3] += st[3]
                yield from jpegs

        with tifffile.TiffWriter(dst, bigtiff=True) as tif:
            tif.write(tiles(), shape=(h, w, 3), dtype="uint8", tile=(th, tw), photometric="rgb", compression="jpeg", description=desc)
    n, exact, maxerr, sumabs = totals
    return dict(pixels=n, exact_pixels=exact, max_error=maxerr, mean_abs_error=sumabs / (3 * n) if n else 0.0, quality=quality)
