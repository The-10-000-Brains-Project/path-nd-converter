/* Emulates OpenSlide's hamamatsu "cunning" tile decode: read header (SOF = whole-strip dims), then overwrite
 * image_width/height with the restart-interval tile dims, then decode.  libjpeg warnings are fatal, as in OpenSlide.
 * usage: cunning FILE TILE_W TILE_H MODE
 *   MODE 0 = exactly what OpenSlide does
 *   MODE 1 = + recompute cinfo->total_iMCU_rows for the tile height before start_decompress
 *   MODE 2 = + cinfo->do_fancy_upsampling = FALSE
 *   MODE 3 = + scale_denom = 8 (DCT scaling; minimum DCT size 1 disables fancy upsampling)
 */
#include <stdio.h>
#include <stdlib.h>
#include <setjmp.h>
#include <jpeglib.h>
struct err { struct jpeg_error_mgr base; jmp_buf env; char msg[JMSG_LENGTH_MAX]; };
static void fatal(j_common_ptr c) { struct err *e = (struct err *)c->err; (*c->err->format_message)(c, e->msg); longjmp(e->env, 1); }
static void emit(j_common_ptr c, int lvl) { if (lvl < 0) fatal(c); }
int main(int argc, char **argv) {
  FILE *f = fopen(argv[1], "rb"); fseek(f, 0, SEEK_END); long n = ftell(f); fseek(f, 0, SEEK_SET);
  unsigned char *buf = malloc(n); fread(buf, 1, n, f); fclose(f);
  int tw = atoi(argv[2]), th = atoi(argv[3]), mode = atoi(argv[4]);
  struct jpeg_decompress_struct cinfo; struct err jerr;
  cinfo.err = jpeg_std_error(&jerr.base); jerr.base.error_exit = fatal; jerr.base.emit_message = emit;
  if (setjmp(jerr.env)) { printf("FAILED: %s\n", jerr.msg); return 2; }
  jpeg_create_decompress(&cinfo); jpeg_mem_src(&cinfo, buf, n);
  jpeg_read_header(&cinfo, TRUE);
  printf("header: SOF %ux%u, max sampling %dx%d, total_iMCU_rows(from header)=%u\n", cinfo.image_width, cinfo.image_height, cinfo.max_h_samp_factor, cinfo.max_v_samp_factor, cinfo.total_iMCU_rows);
  cinfo.image_width = tw; cinfo.image_height = th;                    /* "cunning" */
  if (mode >= 1) cinfo.total_iMCU_rows = (th + cinfo.max_v_samp_factor * 8 - 1) / (cinfo.max_v_samp_factor * 8);
  if (mode >= 2) cinfo.do_fancy_upsampling = FALSE;
  if (mode == 3) { cinfo.scale_num = 1; cinfo.scale_denom = 8; }
  if (mode == 4) { cinfo.scale_num = 1; cinfo.scale_denom = 2; }
  if (mode == 5) { cinfo.scale_num = 1; cinfo.scale_denom = 4; }
  cinfo.out_color_space = JCS_RGB;
  jpeg_start_decompress(&cinfo);
  printf("decoding %ux%u output (need_context_rows effects below)\n", cinfo.output_width, cinfo.output_height);
  JSAMPARRAY row = (*cinfo.mem->alloc_sarray)((j_common_ptr)&cinfo, JPOOL_IMAGE, cinfo.output_width * cinfo.output_components, 1);
  unsigned long sum = 0; FILE *dump = argc > 5 ? fopen(argv[5], "wb") : NULL;
  while (cinfo.output_scanline < cinfo.output_height) { jpeg_read_scanlines(&cinfo, row, 1); for (unsigned i = 0; i < cinfo.output_width * 3; i++) sum += row[0][i]; if (dump) fwrite(row[0], 1, cinfo.output_width * 3, dump); }
  jpeg_finish_decompress(&cinfo);
  printf("OK: decoded %u scanlines, checksum %lu\n", cinfo.output_height, sum);
  return 0;
}
