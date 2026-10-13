"""Command line: inspect, convert, verify."""

from __future__ import annotations

import argparse
import sys

from . import __version__
from .convert import convert, inspect_file
from .jpegbits import Unsupported
from .verify import is_perfect, verify_page


def _inspect(a: argparse.Namespace) -> int:
    rows = inspect_file(a.src)
    print(f"{a.src}: {len(rows)} TIFF pages")
    for r in rows:
        head = f"  page {r['page']}: {r['width']}x{r['height']}  {r['compression']}  {'tiled' if r['tiled'] else 'strips'}"
        if r["convertible"]:
            print(f"{head}  -> convertible ({r['sampling']}, tables {r['tables']})")
        else:
            print(f"{head}  -> NOT convertible: {r['note']}")
    print("\nPick the large JPEG pyramid pages (largest first) for --levels; pass a macro-like page as --macro. "
          "Thumbnails, labels and non-JPEG pages cannot be carried.")
    return 0


def _convert(a: argparse.Namespace) -> int:
    try:
        convert(a.src, a.dst, a.levels, macro=a.macro, repeat=a.repeat, workers=a.workers)
    except Unsupported as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(f"wrote {a.dst}\nEXPERIMENTAL: not tested in Hamamatsu software. Keep the original SVS. Run 'verify' before trusting the result.")
    return 0


def _verify(a: argparse.Namespace) -> int:
    r = verify_page(a.src, a.dst, a.src_page, a.ndpi_page, a.max_tile_rows, a.workers)
    print(f"source page {a.src_page} -> ndpi page {a.ndpi_page}: tile rows {r['tile_rows_checked']}/{r['tile_rows_total']}")
    print(f"  coefficient blocks identical: {r['mcus_equal']}/{r['mcus']}   tiles fully identical: {r['tiles_identical']}/{r['tiles']}")
    print(f"  interior tiles rebuilt byte-for-byte from the NDPI: {r['interior_exact']}/{r['interior']}")
    print("  RESULT:", "PASS" if is_perfect(r) else "FAIL")
    return 0 if is_perfect(r) else 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="path-nd-converter", description="EXPERIMENTAL lossless SVS -> NDPI converter")
    ap.add_argument("--version", action="version", version=__version__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("inspect", help="list the pages of an SVS and which can be converted")
    p.add_argument("src")
    p.set_defaults(fn=_inspect)
    p = sub.add_parser("convert", help="convert pyramid pages of an SVS to an NDPI-style file")
    p.add_argument("src")
    p.add_argument("dst")
    p.add_argument("--levels", type=int, nargs="+", required=True, help="TIFF page indexes of the pyramid levels, largest first")
    p.add_argument("--macro", type=int, default=None, help="TIFF page index of the macro image")
    p.add_argument("--workers", type=int, default=None)
    p.add_argument("--repeat", type=int, default=1, help=argparse.SUPPRESS)  # test only: stack level 0 to exceed 4 GiB
    p.set_defaults(fn=_convert)
    p = sub.add_parser("verify", help="check a converted page against the source, tile by tile")
    p.add_argument("src")
    p.add_argument("dst")
    p.add_argument("--src-page", type=int, required=True)
    p.add_argument("--ndpi-page", type=int, required=True)
    p.add_argument("--max-tile-rows", type=int, default=None, help="sample this many tile rows instead of all")
    p.add_argument("--workers", type=int, default=None)
    p.set_defaults(fn=_verify)
    a = ap.parse_args(argv)
    return a.fn(a)


if __name__ == "__main__":
    raise SystemExit(main())
