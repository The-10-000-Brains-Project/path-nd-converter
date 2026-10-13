import struct

import numpy as np
import pytest
import tifffile

from path_nd_converter.convert import (
    _Out, build_header, convert, inspect_file, page_spec, parse_jpeg, write_ifds,
)
from path_nd_converter.jpegbits import Unsupported
from path_nd_converter.verify import is_perfect, read_ndpi_page, verify_page


def test_inspect_lists_every_page_as_convertible(svs):
    path, _, levels = svs
    rows = inspect_file(path)
    assert len(rows) == 3 and all(r["convertible"] for r in rows)
    assert rows[0]["sampling"] == {"444": "4:4:4", "422": "4:2:2", "420": "4:2:0"}[svs[1]]
    assert "embedded" in rows[0]["tables"]


def test_conversion_is_byte_exact_on_every_level(svs, tmp_path):
    path, _, _ = svs
    out = str(tmp_path / "out.ndpi")
    convert(path, out, [0, 1, 2], workers=2, log=lambda *_: None)
    for k in range(3):
        r = verify_page(path, out, k, k, workers=2)
        assert is_perfect(r), (k, r)
    # the biggest level has interior tiles, so the byte-for-byte path was really exercised
    assert verify_page(path, out, 0, 0, workers=2)["interior"] > 0


def test_tifffile_reads_the_output_as_ndpi_with_padded_sizes(svs, tmp_path):
    path, _, levels = svs
    out = str(tmp_path / "out.ndpi")
    convert(path, out, [0, 1, 2], workers=2, log=lambda *_: None)
    mw, mh = page_spec(path, 0)["mcu_w"], page_spec(path, 0)["mcu_h"]
    with tifffile.TiffFile(out) as t:
        assert t.is_ndpi and len(t.pages) == 3
        for p, lv in zip(t.pages, levels):
            h, w = lv.shape[:2]
            assert (p.imagewidth, p.imagelength) == (-(-w // mw) * mw, -(-h // mh) * mh)


def test_output_does_not_pose_as_a_hamamatsu_scan(svs, tmp_path):
    path, _, levels = svs
    out = str(tmp_path / "out.ndpi")
    convert(path, out, [0, 1, 2], workers=2, log=lambda *_: None)
    with tifffile.TiffFile(out) as t:
        tags = t.pages[0].tags
        for code in (271, 272, 305):
            assert "hamamatsu" not in str(tags[code].value).lower() and "nanozoomer" not in str(tags[code].value).lower()
        assert tags[306].value[:4] == str(__import__("datetime").date.today().year)  # written now, not a fake scan date
        props = tags[65449].value
    props = props.decode() if isinstance(props, bytes) else props
    assert f"Provenance.SourceWidth={levels[0].shape[1]}" in props
    assert f"Provenance.SourceHeight={levels[0].shape[0]}" in props
    assert "NOT produced by a Hamamatsu scanner" in props


def test_openslide_pixels_match_source_for_444(svs, tmp_path):
    openslide = pytest.importorskip("openslide")
    path, name, levels = svs
    if name != "444":
        pytest.skip("only 4:4:4 decodes identically to per-tile decoding; other layouts differ at tile edges")
    out = str(tmp_path / "out.ndpi")
    convert(path, out, [0, 1, 2], workers=2, log=lambda *_: None)
    s = openslide.OpenSlide(out)
    assert s.properties["openslide.vendor"] == "hamamatsu"
    h, w = levels[0].shape[:2]
    got = np.asarray(s.read_region((0, 0), 0, (w, h)).convert("RGB"))
    ref = tifffile.imread(path, key=0)
    assert np.array_equal(got, ref)


def test_rgb_in_jpeg_sources_get_rgb_component_ids(svs):
    path, _, _ = svs
    spec = page_spec(path, 0)
    spec["rgb"] = True  # what Aperio files look like
    assert [c[0] for c in parse_jpeg(build_header(spec, 1008, 720, 63))["sof"]["comps"]] == [ord("R"), ord("G"), ord("B")]
    spec["rgb"] = False
    assert [c[0] for c in parse_jpeg(build_header(spec, 1008, 720, 63))["sof"]["comps"]] == [c[0] for c in spec["comps"]]


def test_unsupported_pages_are_refused_and_leave_no_output(tmp_path):
    from conftest import make_image

    path = tmp_path / "lzw.svs"
    tifffile.imwrite(path, make_image(300, 300), tile=(256, 256), compression="lzw", photometric="rgb")
    with pytest.raises(Unsupported, match="can only hold JPEG"):
        page_spec(str(path), 0)
    out = tmp_path / "out.ndpi"
    with pytest.raises(Unsupported):
        convert(str(path), str(out), [0], workers=1, log=lambda *_: None)
    assert not out.exists()
    assert inspect_file(str(path))[0]["convertible"] is False


def test_directory_encodes_64_bit_values_for_strips_over_4_gib(tmp_path):
    """Real >4 GiB NDPIs split offsets/lengths into low words (entry) and high words (extension block);
    tag 65432 carries the high words of the restart-interval offsets."""
    comps = [(1, 1, 1, 0), (2, 1, 1, 1), (3, 1, 1, 1)]
    big = 4_500_000_000
    starts = [700, 2**32 - 5, 2**32 + 9, big - 1000]
    page = dict(spec=dict(comps=comps), W=1000, H=2_000_000, strip_off=12, strip_len=big, starts=starts)
    path = str(tmp_path / "big.ndpi")
    out = _Out(path)
    out.f.write(b"\x00" * 100)
    write_ifds(out, [page], 0.5, 20.0, {})
    r = read_ndpi_page(path, 0)
    assert r["length"] == big and r["starts"] == starts and r["strip"] == 12 and (r["W"], r["H"]) == (1000, 2_000_000)
    with open(path, "rb") as fh:
        d = fh.read()
    ifd = struct.unpack("<Q", d[4:12])[0]
    n = struct.unpack("<H", d[ifd : ifd + 2])[0]
    tags = {struct.unpack("<H", d[ifd + 2 + 12 * i : ifd + 4 + 12 * i])[0] for i in range(n)}
    assert 65432 in tags
