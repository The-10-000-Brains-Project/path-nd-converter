# path-nd-converter

Converts an Aperio `.svs` slide into an NDPI-style file without recompressing the image, then checks the
result tile by tile against the source.

This is experimental. The output has been opened in OpenSlide and tifffile, but never in Hamamatsu's own
software. Keep your original SVS files.

## How it works

An SVS stores a slide as many small, independent JPEG tiles. An NDPI stores each pyramid level as one big JPEG
with restart markers. This tool moves the compressed data from the first layout into the second. The Huffman
codes for each block are copied unchanged. Only the DC values are re-encoded, because a restart marker resets
them. No pixel is decoded and nothing is re-quantised. JPEG 2000 slides are the exception: they
are refused unless you opt in to a re-encode that is close but not exact (see below).

## Usage

```bash
pip install -e ".[dev]"
```

See which pages of the SVS can be converted:

```console
$ path-nd-converter inspect CMU-1-Small-Region.svs
CMU-1-Small-Region.svs: 4 TIFF pages
  page 0: 2220x2967  JPEG  tiled  -> convertible (4:4:4, tables shared (JPEGTables))
  page 1: 574x768  JPEG  strips  -> convertible (4:4:4, tables shared (JPEGTables))
  page 2: 387x463  LZW  strips  -> NOT convertible: TIFF compression 5 (LZW); NDPI can only hold JPEG
  page 3: 1280x431  JPEG  strips  -> convertible (4:4:4, tables shared (JPEGTables))
```

Convert the pyramid levels (largest first, by page number) and the macro image:

```console
$ path-nd-converter convert CMU-1-Small-Region.svs small.ndpi --levels 0 --macro 3
page 0: 2220x2967 -> 2224x2968, 4:4:4, 8x8 MCU, restart interval 139, 1.2 MB
macro page 3: 1280x432
wrote small.ndpi
```

Check each converted page against the source. This rebuilds every original tile's compressed bytes from the
NDPI file and compares them. The exit code is 0 on a pass and 1 on a failure.

```console
$ path-nd-converter verify CMU-1-Small-Region.svs small.ndpi --src-page 0 --ndpi-page 0
source page 0 -> ndpi page 0: tile rows 13/13
  coefficient blocks identical: 103138/103138   tiles fully identical: 130/130
  interior tiles rebuilt byte-for-byte from the NDPI: 108/108
  RESULT: PASS
```

A page it can't carry is refused, and no output file is left behind:

```console
$ path-nd-converter convert JP2K-33003-1.svs out.ndpi --levels 0
error: TIFF compression 33003 (APERIO_JP2000_YCBC); NDPI can only hold JPEG
```

### JPEG 2000 slides (opt-in, not exact)

NDPI can only hold JPEG, so JPEG 2000 pixels can't be carried exactly. With `--lossy-jpeg2000` the tool decodes
each tile and re-encodes it as a JPEG that stores RGB directly, at quality 100, which gets very close but not
equal. It measures how close while it works and writes the result into the output file.

```console
$ path-nd-converter convert JP2K-33003-1.svs out.ndpi --levels 0 2 3 --macro 5 --lossy-jpeg2000
page 0: re-encoding JPEG 2000 as RGB JPEG, quality 100 (not exact)
  pixels exactly equal to the JPEG 2000 decode: 75.82%, largest error 2, mean absolute error 0.088
page 2: re-encoding JPEG 2000 as RGB JPEG, quality 100 (not exact)
  pixels exactly equal to the JPEG 2000 decode: 83.00%, largest error 2, mean absolute error 0.068
page 3: re-encoding JPEG 2000 as RGB JPEG, quality 100 (not exact)
  pixels exactly equal to the JPEG 2000 decode: 82.22%, largest error 2, mean absolute error 0.070
page 0: 15374x17497 -> 15376x17504, 4:4:4, 8x8 MCU, restart interval 62, 661.7 MB
...
```

That slide went from 64 MB to 694 MB. Add `--workdir DIR` to keep the re-encoded intermediate JPEG files; `verify`
can then check the NDPI against them, which shows the second step (JPEG to NDPI) is still byte-exact even though
the first step (JPEG 2000 to JPEG) is not.

The sizes in the first example went from 2220x2967 to 2224x2968. NDPI readers need the image to be a whole number of
JPEG blocks across and down, so the converter pads the edges. The original size is saved in the file (see
Known problems, item 4).

## What has been checked

On two real Aperio slides from the OpenSlide test data (`CMU-1.svs`, 3 levels up to 46000x32914, and
`CMU-3.svs`, 4 levels up to 66000x45402), every level passes `verify`: about 75 million coefficient blocks are
identical to the source, and about 73,000 interior tiles come back byte for byte. The macro images pass too.
For 4:4:4 data the decoded pixels also match the source. They were compared in full on the smaller levels and
on a 148-million-pixel sample of the largest.

Three things were only checked on synthetic files built from real tissue pixels, because no real sample was
available: 4:2:2 and 4:2:0 chroma, tables stored inside each tile instead of shared, and a 4.4 GB strip using
the 64-bit layout of a real over-4-GiB scanner file. OpenSlide read rows from throughout that last file with
no pixel differences.

The largest levels were checked with the scratch scripts this package was built from, which use the same
logic. The packaged `verify` has been run on the smaller levels, the macro images, the small slide above, and
the slides `pytest` builds. `pytest` runs these checks on small slides it builds itself.

For JPEG 2000, one real slide (`JP2K-33003-1.svs`, 15374x17497) was converted with `--lossy-jpeg2000`. The NDPI is
byte-exact against the re-encoded tiles on every level (4.2 million blocks on the top level), and the pixels read
through OpenSlide are 75.9% exactly equal to the original JPEG 2000 decode, with a largest error of 2. A second
sample (`CMU-1-JP2K-33005.svs`, RGB JPEG 2000) was tested for the re-encoding step only: 86.7% exact, largest
error 2.

## Known problems

1. Only open-source readers have been tried: OpenSlide, tifffile and the libjpeg tools. The NDPI layout comes
   from OpenSlide's source and three real NDPI files. There is no specification.
2. Colour relies on a libjpeg convention. Aperio stores RGB values in its JPEGs, not YCbCr. The output labels
   the components `R`, `G`, `B`, which makes libjpeg-based decoders read them as RGB. A decoder that assumes
   YCbCr for any three-component JPEG would show wrong colours (white turns pink). Only libjpeg-based
   decoders were tested.
3. Stock OpenSlide can't read 4:2:0 or 4:4:0 output at full resolution. It overwrites the image height after
   libjpeg has read the header, so libjpeg reads ahead past the end of the tile. 4:4:4, 4:2:2 and 4:1:1 are
   fine, and tifffile reads 4:2:0 correctly. `patches/openslide-4.0.1-ndpi-subsampled-chroma.patch` fixes it:
   on a v4.0.1 build, 414 of 414 regions from files that already worked were byte-identical to the unpatched
   build, and 4:2:0 now reads. It also applies to current `main`, and a patched `main` build reads the 4:2:0
   files (that build got a smaller check). It hasn't been sent upstream. With the patch, the first and last
   pixel row of each 16-row band differ slightly from a continuous decode.
4. Image sizes change by up to 7 pixels (15 at 4:2:0). The original size is stored in the `Provenance.*`
   properties (tag 65449), which OpenSlide shows as `hamamatsu.Provenance.*`.
5. Subsampled chroma decodes a little differently. The coefficients are identical, but a reader that decodes the
   single stream interpolates colour across tile edges, where a per-tile decode can't. About 0.2% to 0.4% of
   pixels differ, all on source-tile edge rows and columns. A non-interpolating upsampler gives identical pixels.
6. JPEG 2000 slides can't be converted exactly. NDPI holds only JPEG, so by default these pages are refused.
   With `--lossy-jpeg2000` they are re-encoded as RGB JPEG at quality 100. On the two samples tried, 76% to 87% of
   pixels end up exactly equal and the largest difference is 2 (out of 255), but the other pixels do differ and the
   data grows 8 to 11 times. Re-encoding through YCbCr instead is much worse (9% to 43% exact, largest error 4 to 5).
   Quality 95 shrinks the data to 4.6 to 6 times but allows errors up to 11. A lossless JPEG is bit-exact and
   OpenSlide can read one, but only when it has no restart markers, which makes each level a single tile that must
   be decoded whole. That works for small levels and not for real slides (see `docs/FINDINGS.md`).
7. The label and thumbnail images aren't carried over, because NDPI has no place for them. ICC profiles and
   Aperio's own metadata aren't either, apart from the description text kept in `Provenance.SourceDescription`.
8. Some values are placeholders. Slide-centre offsets and the focal plane are 0. Make, Model and Software name
   this tool, and the date is when the file was written. The file says in its properties that it was not
   produced by a Hamamatsu scanner.
9. Scale is only partly tested. It is pure Python. The 1.5-gigapixel `CMU-1.svs` (156 MB of JPEG at the top level)
   converts in 41 seconds on a 10-core Mac, with the main process peaking near 200 MB, and the 270-megapixel JPEG 2000
   slide in 103 seconds. The converter holds one level's compressed data in memory, so a level of several gigabytes
   would need that much RAM. The largest real source converted was about 250 MB.
10. It stops with an error on progressive or 12-bit JPEG, non-JPEG pages, missing or zero-length tiles,
    quantisation tables that differ between tiles of one page, and tile sizes that aren't a whole number of
    JPEG blocks.

## Files

- `src/path_nd_converter/convert.py`: the converter
- `src/path_nd_converter/verify.py`: the tile-level verifier
- `patches/`: the OpenSlide patch
- `tools/cunning.c`: a C program that reproduces OpenSlide's tile decode, used to find the 4:2:0 failure
- `docs/FINDINGS.md`: the NDPI layout as observed, and the evidence behind the 4:2:0 diagnosis

## License

MIT, see `LICENSE`. The patch in `patches/` changes OpenSlide, so it stays under OpenSlide's license,
LGPL-2.1.
