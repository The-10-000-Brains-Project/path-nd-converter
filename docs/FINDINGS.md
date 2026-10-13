# Findings

What was learned while building the converter. Everything here comes from reading OpenSlide's source
(`openslide-vendor-hamamatsu.c`, `openslide-decode-tifflike.c`, `openslide-decode-jpeg.c`, v4.0.1 and main),
libjpeg-turbo's source, and dissecting three real NDPI files (OpenSlide test data: `CMU-1.ndpi`, `OS-1.ndpi`,
`Hamamatsu-1.ndpi`, the last 6.9 GB). There is no Hamamatsu specification; treat the layout below as observed,
not guaranteed.

## NDPI file layout (observed)

An NDPI is a classic TIFF with 64-bit extensions:

- **Header, 12 bytes:** `II*\0`, then the first-IFD offset as an 8-byte little-endian value.
- **Each IFD:** `u16 entry count`, then `12 × count` bytes of normal entries, then **an 8-byte next-IFD offset**,
  then **`4 × count` bytes of value extensions**. The extension for entry *i* holds the high 32 bits of that
  entry's value or offset. The next-IFD pointer comes *before* the extension block. Getting this order wrong is
  invisible in single-IFD files (both are zeros) and breaks every multi-page file.
- **One JPEG strip per pyramid level** (`StripOffsets`/`StripByteCounts`, count 1). Lengths and offsets use
  the 64-bit scheme: real 6.9 GB file, `StripByteCounts` low word 506,102,801 + high word 1 = 4,801,070,097.
- **Tag 65420** is the NDPI marker; OpenSlide detects NDPI by it. **65421** is source lens (magnification, float;
  `-1` marks the macro image). **65422/65423** are slide-centre offsets, **65424** the focal plane (OpenSlide
  skips levels whose focal plane is not 0). **65449** is a text property map. **65426** is an array of the
  byte offsets of every restart interval, relative to the strip start; **65432** holds their high 32 bits and is
  present only when the strip exceeds 4 GiB. **65425** and **65428** exist in real files; their meaning is unknown
  and this converter does not write them.
- **The JPEG:** baseline, one scan, `DRI` set, restart markers `RST0..RST7` cycling. Each restart interval is a
  horizontal run of whole MCUs inside one MCU row. Dimensions that JPEG cannot express (over 65,500) are written
  as 0 in the `SOF`; OpenSlide substitutes 65,500.

### Constraints OpenSlide enforces on that JPEG

- The restart interval must divide the MCUs per row exactly, and the image width must be a multiple of the
  interval's pixel width. Otherwise rows are misaligned (we saw a black right half and shifted rows). The
  converter therefore pads image sizes up to a whole number of MCUs and picks the largest interval ≤256 MCUs that
  divides the row, falling back to one interval per row.
- The JPEG's own size must equal the TIFF size, unless either dimension exceeds 65,500.
- A restart marker must precede every recorded interval offset.
- Levels must be in decreasing width order, and consistent in aspect ratio (a stacked tall first level next to a
  normal second page was rejected: "Downsampled images not correctly ordered").

## Why colour needed a trick

Aperio SVS files store RGB values directly in the JPEG components, and the TIFF says `Photometric=RGB`. The
JPEG itself carries no colour-space marker. A decoder that sees component ids `0,1,2` assumes YCbCr, so white
(244,242,242) decodes as (255,123,255). libjpeg treats ids `R`,`G`,`B` as RGB when there is no JFIF/Adobe
marker, so the converter relabels them. This is a libjpeg heuristic, not a standard; other decoders may ignore it.

## The 4:2:0 failure in stock OpenSlide

Symptom: reading a 4:2:0 or 4:4:0 NDPI at full resolution fails with
`Corrupt JPEG data: found marker 0xd9 instead of RST0`. Scaled reads (1/2, 1/4, 1/8) work; 4:4:4, 4:2:2 and
4:1:1 work.

Cause, each step checked in source:

1. OpenSlide decodes one restart interval at a time: header + interval, with the last byte replaced by EOI. After
   `jpeg_read_header` (which sees the whole strip's size in the `SOF`) it overwrites `cinfo->image_width/height`
   with the tile's size (`// cunning`).
2. libjpeg computed `total_iMCU_rows` when it read the header (`jdinput.c`). The overwrite does not update it, so
   libjpeg believes more MCU rows follow.
3. libjpeg's h2v2 (and h1v2) fancy upsampler needs the next row for context (`jdsample.c`,
   `need_context_rows = TRUE`), so `jdmainct.c` reads ahead into a row the slice does not contain.
4. It reaches EOI where a restart marker was expected and warns. OpenSlide converts every libjpeg warning to a
   fatal error (`decode-jpeg.c`, `my_emit_message`).

Controlled experiment (`tools/cunning.c`, a C program that does what OpenSlide does), 4:2:0 tile:

| change | result |
|---|---|
| exactly as OpenSlide | fails with the same message |
| + recompute `total_iMCU_rows` for the tile | decodes |
| + disable fancy upsampling | decodes |
| DCT scaling 1/2, 1/4 or 1/8 | decodes |

4:2:2 decodes in every mode. Across `cjpeg` layouts 1x1, 2x1, 4x1 pass and 1x2, 2x2 fail, matching
`need_context_rows`.

### The patch

`patches/openslide-4.0.1-ndpi-subsampled-chroma.patch` writes the tile's real size into the `SOF` before libjpeg
parses it, instead of overwriting afterwards. If the tile is wider than JPEG can express it keeps the old
override. Results: unpatched build fails on 4:2:0, patched reads it; 414 of 414 regions from real and converted
files that already worked are byte-identical between the two builds on v4.0.1.

The fixed reader's 4:2:0 output differs from a continuous decode by 3–4% of pixels (max 78), all on the first or
last pixel row of each 16-row band, because each band is decoded alone with no row above or below. Decoding the
neighbouring rows too would remove that at about 3× the decode cost; not done.

A file-side workaround exists only for images over 65,500 pixels in some dimension, where OpenSlide skips the
size comparison: writing a small `SOF` height (one MCU row) makes full-resolution 4:2:0 reads work. It does not
work for smaller images.

## Chroma subsampling and pixel equality

Coefficients are identical in every layout. Decoded pixels are identical in 4:4:4. For 4:2:2 and 4:2:0, a
non-interpolating upsampler gives identical pixels; libjpeg's default interpolating one differs on 0.18% (4:2:2)
and 0.37% (4:2:0) of pixels, all on the source's tile-edge rows and columns, because the source is decoded one
tile at a time and the converted stream is decoded continuously.

## JPEG 2000

JPEG 2000 SVS pages (TIFF compression 33003/33005) cannot be carried: NDPI readers decode JPEG only. Re-encoding
one page of `JP2K-33003-1.svs` as 4:4:4 JPEG: quality 100 → 90.6% of pixels change (max error 4, PSNR 51 dB),
data grows from 2.36 MB to 15.6 MB.

## Bugs found on the way (each fixed)

- NDPI IFD order (next pointer before extension block); invisible until a second page existed.
- JPEG size must equal TIFF size; widths must be multiples of the interval width.
- tifffile reads NDPI first-IFD offsets as 64-bit only when the file name ends in `.ndpi`.
- A first pixel comparison used OpenSlide's Aperio reader as the reference. It disagrees with direct libjpeg and
  tifffile decodes on the *source SVS itself* (0.1–4.5% of pixels per tile at fractional-downsample levels), so
  direct decodes are the reference.
