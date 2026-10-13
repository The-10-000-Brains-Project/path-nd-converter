# path-nd-converter

**EXPERIMENTAL.** Converts an Aperio `.svs` slide into an NDPI-style file without recompressing the image, and
checks the result tile by tile against the source.

Do not use the output to replace originals, for archiving, or for clinical or regulated work. It has not been
opened in Hamamatsu software. Keep the original SVS.

## What it does

SVS and NDPI both store the image as JPEG. An SVS keeps many small independent JPEG tiles; an NDPI keeps one
JPEG per pyramid level, with restart markers every few hundred blocks. This tool re-lays the compressed data
into the NDPI arrangement. Each block's Huffman symbols are copied as they are, and only the DC values are
re-encoded (restart markers reset DC prediction). Nothing is decoded to pixels or re-quantised.

```bash
pip install -e ".[dev]"

path-nd-converter inspect slide.svs                       # which pages can be converted
path-nd-converter convert slide.svs slide.ndpi --levels 0 2 3 --macro 6
path-nd-converter verify  slide.svs slide.ndpi --src-page 0 --ndpi-page 0
```

`--levels` takes the TIFF page numbers of the pyramid levels, largest first. `inspect` shows them.
`verify` rebuilds each original tile's compressed bytes from the NDPI file and compares; run it per level.

## What has been verified

On real Aperio slides from the OpenSlide test data (`CMU-1.svs`, 3 levels, 46000×32914; `CMU-3.svs`, 4 levels,
66000×45402), every level:

- 100% of the retained coefficient blocks are identical to the source (about 75 million blocks across those two files).
- 100% of interior tiles, rebuilt from the NDPI, are byte-for-byte the original tile data (about 73,000 tiles).
- Decoded pixels are identical to the source for 4:4:4 data (checked in full on the smaller levels; the 46000×32914
  level was sampled: 148 million pixels, 0 differing).
- The macro image is carried over and is byte-exact.

Also tested, on synthetic data built from real tissue pixels (no real sample was available):
4:2:2 and 4:2:0 chroma, tables embedded in each tile instead of shared, and a 4.4 GB strip using the 64-bit
layout of a real >4 GiB scanner file (OpenSlide read rows from throughout it, 0 pixels differing).

The unit tests (`pytest`) build their own small slides and cover the same checks.

## Known problems

Read these before using the output for anything.

1. **Only open-source readers have been tried** (OpenSlide, tifffile, libjpeg tools). Hamamatsu's own software
   has not. The NDPI layout is reverse-engineered from OpenSlide's reader and three real NDPI files, with no spec.
2. **Colour depends on a heuristic.** Aperio stores RGB values in its JPEGs, not YCbCr. The output labels the
   components `R`,`G`,`B` so libjpeg-based decoders read them as RGB. A decoder that assumes YCbCr for any
   three-component JPEG would show wrong colours (white turns pink). Only libjpeg-based decoders were tested.
3. **Stock OpenSlide cannot read 4:2:0 output at full resolution.** Its tile decoder overwrites the image height
   after libjpeg has read the header, and libjpeg then reads ahead past the end of the tile. 4:2:0 and 4:4:0
   fail; 4:4:4, 4:2:2 and 4:1:1 work. `patches/openslide-4.0.1-ndpi-subsampled-chroma.patch` fixes it. On a v4.0.1
   build, 414 of 414 regions read from files that already worked were byte-identical to the unpatched build, and
   4:2:0 now reads. The patch applies to current main, and a patched main build also reads the 4:2:0 files (that
   build got a smaller check). It has not been submitted upstream, and the fixed reader's 4:2:0 pixels differ
   slightly from a continuous decode on the first and last row of each 16-row band. See `docs/FINDINGS.md`.
   tifffile reads 4:2:0 output correctly.
4. **Image sizes change.** Sizes are padded up to a whole number of MCUs (up to 7 pixels at 4:4:4, 15 at 4:2:0).
   The original size is stored in the `Provenance.*` properties (tag 65449, shown by OpenSlide as
   `hamamatsu.Provenance.*`).
5. **Subsampled chroma decodes slightly differently.** Coefficients are identical, but a reader that decodes the
   single stream interpolates chroma across tile edges where a per-tile decode cannot. About 0.2–0.4% of pixels
   differ, all on source-tile edge rows and columns. With a non-interpolating upsampler they are identical.
6. **JPEG 2000 SVS cannot be converted exactly.** NDPI only holds JPEG. Re-encoding as JPEG changes 90.6% of
   pixels even at quality 100 (max error 4) and makes the data 6.6× larger. These pages are refused.
7. **Not carried over:** label and thumbnail images (NDPI has no slot for them), ICC profiles, Aperio-specific
   metadata beyond the description string kept in `Provenance.SourceDescription`.
8. **Made-up values that readers may display:** slide-centre offsets and focal plane are 0. Make/Model/Software
   say this tool wrote the file, and the date is when it was written. The file carries
   `Provenance.Notice=...NOT produced by a Hamamatsu scanner`.
9. **Not tested at scale.** It is pure Python (about 10 minutes for a 1.5-gigapixel slide on 10 cores) and holds
   one level's compressed data in memory. No real slide over about 250 MB was converted.
10. **Refuses, with an error:** progressive or 12-bit JPEG, non-JPEG pages, missing or zero-length tiles, tiles
    whose quantisation tables differ within a page, chunk sizes that are not a whole number of MCUs.

## Layout

- `src/path_nd_converter/convert.py` – the converter
- `src/path_nd_converter/verify.py` – tile-level verifier
- `patches/` – OpenSlide patch for 4:2:0 NDPI (LGPL-2.1, same as OpenSlide)
- `tools/cunning.c` – C program reproducing OpenSlide's tile decode, used to isolate the 4:2:0 failure
- `docs/FINDINGS.md` – what was learned about the NDPI layout, with evidence

No license has been chosen for this repository yet.
