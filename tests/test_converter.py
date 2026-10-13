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


# ---------------------------------------------------------------- opt-in JPEG 2000 path
@pytest.fixture
def j2k_svs(tmp_path):
    from conftest import make_image

    img = make_image(700, 530, seed=3)
    path = tmp_path / "j2k.svs"
    with tifffile.TiffWriter(path) as tif:
        for k, a in enumerate([img, img[::2, ::2]]):
            tif.write(a, tile=(256, 256), photometric="rgb", compression="jpeg2000", subfiletype=0 if k == 0 else 1,
                      description="Aperio synthetic JP2K | AppMag = 20 | MPP = 0.5" if k == 0 else None)
    return str(path), img


def test_jpeg2000_is_refused_by_default_with_a_hint(j2k_svs, tmp_path):
    path, _ = j2k_svs
    out = tmp_path / "out.ndpi"
    with pytest.raises(Unsupported, match="--lossy-jpeg2000"):
        convert(path, str(out), [0, 1], workers=2, log=lambda *_: None)
    assert not out.exists()
    assert "--lossy-jpeg2000" in inspect_file(path)[0]["note"]


def test_lossy_jpeg2000_is_near_lossless_byte_exact_against_its_intermediate_and_says_so(j2k_svs, tmp_path):
    path, img = j2k_svs
    out, work = str(tmp_path / "out.ndpi"), str(tmp_path / "work")
    convert(path, out, [0, 1], workers=2, lossy_jpeg2000=True, workdir=work, log=lambda *_: None)
    # the JPEG -> NDPI step is still byte exact against the re-encoded tiles
    for k in range(2):
        assert is_perfect(verify_page(f"{work}/page{k}.jpeg-reencoded.tif", out, 0, k, workers=2)), k
    with tifffile.TiffFile(out) as t:
        assert t.is_ndpi
        props = t.pages[0].tags[65449].value
    props = props.decode() if isinstance(props, bytes) else props
    # the output admits it is not identical, and records how far off it is
    assert "pixels NOT identical" in props and "Jpeg2000.Page0.MaxPixelError=" in props
    max_err = int(props.split("Jpeg2000.Page0.MaxPixelError=")[1].split("\r")[0])
    assert max_err <= 3  # quality 100 RGB-in-JPEG: small, not zero
    exact = float(props.split("Jpeg2000.Page0.ExactPixelsPercent=")[1].split("\r")[0])
    assert 40 < exact < 100  # most pixels equal, but it is not bit-exact


def test_lossy_jpeg2000_pixels_through_openslide_match_the_recorded_error(j2k_svs, tmp_path):
    openslide = pytest.importorskip("openslide")
    path, _ = j2k_svs
    out = str(tmp_path / "out.ndpi")
    convert(path, out, [0, 1], workers=2, lossy_jpeg2000=True, log=lambda *_: None)
    ref = tifffile.imread(path, key=0)
    h, w = ref.shape[:2]
    got = np.asarray(openslide.OpenSlide(out).read_region((0, 0), 0, (w, h)).convert("RGB"))
    assert int(np.abs(got.astype(int) - ref.astype(int)).max()) <= 3


def test_decoder_cost_does_not_grow_with_tile_size():
    """Regression: reading bits through one huge integer made decoding quadratic in tile size, so
    quality-100 tiles (~90 KB) took ~50x longer than ordinary ones. Large tiles must stay cheap."""
    import time

    import imagecodecs

    from conftest import make_image
    from path_nd_converter.convert import iter_mcu_rows

    tile = make_image(256, 256, seed=9)
    enc = imagecodecs.jpeg8_encode(tile, level=100, colorspace="rgb", outcolorspace="ycbcr", subsampling="444")
    assert len(enc) > 60_000  # a genuinely large tile
    spec = dict(comps=parse_jpeg(enc)["sof"]["comps"], mcu_w=8, mcu_h=8, bmap=[0, 1, 2], dqt=parse_jpeg(enc)["dqt"], dht=parse_jpeg(enc)["dht"])
    t0 = time.time()
    rows = list(iter_mcu_rows(enc, spec))
    assert len(rows) == 32 and time.time() - t0 < 2.0  # was ~20 s with the quadratic reader
