/* Drawing on the Nook's screen.
 *
 * The panel is 1024x600 landscape, but we use it in portrait (600x1024).
 * Instead of rotating every frame, every canvas stores pixels already in
 * the panel's layout: portrait (x, y) lives at px[(w - 1 - x) * pitch + y].
 * That makes the screen back buffer identical to the framebuffer, and a
 * vertical scroll becomes one memcpy per panel row.
 */
#ifndef GFX_H
#define GFX_H

#include <stdint.h>

typedef uint16_t px_t; /* RGB565 */

#define RGB(r, g, b) ((px_t)((((r) & 0xf8) << 8) | (((g) & 0xfc) << 3) | ((b) >> 3)))

typedef struct {
    px_t *px;
    int w, h;  /* portrait size */
    int pitch; /* pixels between panel rows (>= h) */
} canvas_t;

typedef struct font font_t;

/* set to 1 if the panel turns out to be mirrored left-to-right */
extern int gfx_mirror;
#define PANEL_ROW(c, x) (gfx_mirror ? (x) : (c)->w - 1 - (x))

int  canvas_init(canvas_t *c, int w, int h);
void canvas_free(canvas_t *c);

void fill_rect(canvas_t *c, int x, int y, int w, int h, px_t col);
void fill_round(canvas_t *c, int x, int y, int w, int h, int r, px_t col);
void frame_rect(canvas_t *c, int x, int y, int w, int h, int t, px_t col);
/* copy src rows [sy, sy+h) to dst rows [dy, dy+h), full width */
void blit_rows(canvas_t *dst, int dy, const canvas_t *src, int sy, int h);

/* fonts */
font_t *font_load(const char *path, int px_size);
int  font_height(font_t *f);  /* line height */
int  font_ascent(font_t *f);
int  text_width(font_t *f, const char *s, int n); /* n < 0: whole string */
/* draws one line; returns x after the text */
int  draw_text(canvas_t *c, font_t *f, int x, int y, const char *s, int n, px_t col);
/* byte length of the longest prefix of s that fits in width w, breaking at
 * spaces when possible; *next is set to where the next line starts */
int  wrap_line(font_t *f, const char *s, int w, const char **next);

/* utf-8 */
uint32_t utf8_next(const char **s);

#endif
