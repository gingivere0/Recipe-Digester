#include "gfx.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#define STB_TRUETYPE_IMPLEMENTATION
#define STBTT_STATIC
#include "third_party/stb_truetype.h"

#define STB_IMAGE_IMPLEMENTATION
#define STB_IMAGE_STATIC
#define STBI_ONLY_JPEG
#define STBI_ONLY_PNG
#define STBI_NO_HDR
#define STBI_NO_LINEAR
#if defined(__ARM_NEON)
#define STBI_NEON
#endif
#include "third_party/stb_image.h"

int gfx_mirror;

int canvas_init(canvas_t *c, int w, int h)
{
    c->w = w;
    c->h = h;
    c->pitch = h;
    c->px = malloc((size_t)w * h * sizeof(px_t));
    return c->px ? 0 : -1;
}

void canvas_free(canvas_t *c)
{
    free(c->px);
    c->px = NULL;
}

static int clip(canvas_t *c, int *x, int *y, int *w, int *h)
{
    if (*x < 0) { *w += *x; *x = 0; }
    if (*y < 0) { *h += *y; *y = 0; }
    if (*x + *w > c->w) *w = c->w - *x;
    if (*y + *h > c->h) *h = c->h - *y;
    return *w > 0 && *h > 0;
}

/* pointer to the panel row holding portrait column x, at portrait row y */
static inline px_t *at(canvas_t *c, int x, int y)
{
    return c->px + (size_t)PANEL_ROW(c, x) * c->pitch + y;
}

static inline void fill_run(px_t *p, int n, px_t col)
{
    uint32_t two = col | ((uint32_t)col << 16);
    if (n > 0 && ((uintptr_t)p & 2)) { *p++ = col; n--; }
    uint32_t *q = (uint32_t *)p;
    for (; n >= 2; n -= 2) *q++ = two;
    if (n) *(px_t *)q = col;
}

void fill_rect(canvas_t *c, int x, int y, int w, int h, px_t col)
{
    if (!clip(c, &x, &y, &w, &h)) return;
    for (int i = 0; i < w; i++) fill_run(at(c, x + i, y), h, col);
}

void fill_round(canvas_t *c, int x, int y, int w, int h, int r, px_t col)
{
    if (r * 2 > h) r = h / 2;
    if (r * 2 > w) r = w / 2;
    for (int i = 0; i < w; i++) {
        /* d: horizontal distance into the corner circle at this column */
        int d = i < r ? r - i : (i >= w - r ? i - (w - r - 1) : 0);
        int inset = 0;
        while (d > 0 && inset < r && (r - inset) * (r - inset) + d * d > r * r) inset++;
        int yy = y + inset, hh = h - 2 * inset, xx = x + i, ww = 1;
        if (clip(c, &xx, &yy, &ww, &hh)) fill_run(at(c, xx, yy), hh, col);
    }
}

void frame_rect(canvas_t *c, int x, int y, int w, int h, int t, px_t col)
{
    fill_rect(c, x, y, w, t, col);
    fill_rect(c, x, y + h - t, w, t, col);
    fill_rect(c, x, y, t, h, col);
    fill_rect(c, x + w - t, y, t, h, col);
}

void blit_rows(canvas_t *dst, int dy, const canvas_t *src, int sy, int h)
{
    if (sy < 0) { dy -= sy; h += sy; sy = 0; }
    if (sy + h > src->h) h = src->h - sy;
    if (dy < 0) { sy -= dy; h += dy; dy = 0; }
    if (dy + h > dst->h) h = dst->h - dy;
    if (h <= 0) return;
    int w = dst->w < src->w ? dst->w : src->w;
    for (int r = 0; r < w; r++)
        memcpy(dst->px + (size_t)r * dst->pitch + dy,
               src->px + (size_t)r * src->pitch + sy, (size_t)h * sizeof(px_t));
}

/* ---- images ---- */

px_t *image_load(const char *path, int max_w, int max_h, int *w, int *h)
{
    int iw, ih, n;
    unsigned char *rgb = stbi_load(path, &iw, &ih, &n, 3);
    if (!rgb) return NULL;
    int ow = iw, oh = ih;
    if (ow > max_w) { oh = (int)((long long)oh * max_w / ow); ow = max_w; }
    if (oh > max_h) { ow = (int)((long long)ow * max_h / oh); oh = max_h; }
    if (ow < 1) ow = 1;
    if (oh < 1) oh = 1;
    px_t *out = malloc((size_t)ow * oh * sizeof(px_t));
    if (!out) { stbi_image_free(rgb); return NULL; }
    /* 4x4 ordered dither hides banding in 16-bit color */
    static const int bayer[4][4] = { { 0, 8, 2, 10 }, { 12, 4, 14, 6 }, { 3, 11, 1, 9 }, { 15, 7, 13, 5 } };
    for (int y = 0; y < oh; y++) {
        int sy0 = (int)((long long)y * ih / oh), sy1 = (int)((long long)(y + 1) * ih / oh);
        if (sy1 <= sy0) sy1 = sy0 + 1;
        for (int x = 0; x < ow; x++) {
            /* average the source pixels this output pixel covers */
            int sx0 = (int)((long long)x * iw / ow), sx1 = (int)((long long)(x + 1) * iw / ow);
            if (sx1 <= sx0) sx1 = sx0 + 1;
            unsigned r = 0, g = 0, b = 0, cnt = 0;
            for (int sy = sy0; sy < sy1; sy++) {
                const unsigned char *p = rgb + ((size_t)sy * iw + sx0) * 3;
                for (int sx = sx0; sx < sx1; sx++, p += 3) { r += p[0]; g += p[1]; b += p[2]; cnt++; }
            }
            int d = bayer[y & 3][x & 3];
            int rr = (int)(r / cnt) + d / 2 - 4, gg = (int)(g / cnt) + d / 4 - 2, bb = (int)(b / cnt) + d / 2 - 4;
            rr = rr < 0 ? 0 : rr > 255 ? 255 : rr;
            gg = gg < 0 ? 0 : gg > 255 ? 255 : gg;
            bb = bb < 0 ? 0 : bb > 255 ? 255 : bb;
            out[(size_t)y * ow + x] = RGB(rr, gg, bb);
        }
    }
    stbi_image_free(rgb);
    *w = ow;
    *h = oh;
    return out;
}

void draw_image(canvas_t *c, int x, int y, const px_t *img, int w, int h)
{
    int x0 = x, y0 = y, cw = w, ch = h;
    if (!clip(c, &x0, &y0, &cw, &ch)) return;
    for (int i = 0; i < cw; i++) {
        px_t *p = at(c, x0 + i, y0);
        const px_t *s = img + (size_t)(y0 - y) * w + (x0 - x + i);
        for (int j = 0; j < ch; j++, s += w) p[j] = *s;
    }
}

/* ---- utf-8 ---- */

uint32_t utf8_next(const char **sp)
{
    const unsigned char *s = (const unsigned char *)*sp;
    uint32_t c = *s++;
    int extra = 0;
    if (c >= 0xf0) { c &= 7; extra = 3; }
    else if (c >= 0xe0) { c &= 15; extra = 2; }
    else if (c >= 0xc0) { c &= 31; extra = 1; }
    else if (c >= 0x80) c = 0xfffd;
    while (extra-- && (*s & 0xc0) == 0x80) c = (c << 6) | (*s++ & 63);
    *sp = (const char *)s;
    return c;
}

/* ---- fonts ---- */

typedef struct {
    uint32_t cp;
    int adv, x0, y0, w, h;
    unsigned char *bmp;
} glyph_t;

#define GLYPH_SLOTS 512

struct font {
    stbtt_fontinfo info;
    unsigned char *data;
    float scale;
    int ascent, height;
    glyph_t slots[GLYPH_SLOTS]; /* open-addressed cache */
};

font_t *font_load(const char *path, int px_size)
{
    static struct { char path[256]; unsigned char *data; } files[4];
    unsigned char *data = NULL;
    for (int i = 0; i < 4 && files[i].data; i++)
        if (!strcmp(files[i].path, path)) data = files[i].data;
    if (!data) {
        FILE *f = fopen(path, "rb");
        if (!f) return NULL;
        fseek(f, 0, SEEK_END);
        long n = ftell(f);
        fseek(f, 0, SEEK_SET);
        data = malloc(n);
        if (!data || fread(data, 1, n, f) != (size_t)n) { fclose(f); free(data); return NULL; }
        fclose(f);
        for (int i = 0; i < 4; i++)
            if (!files[i].data) {
                snprintf(files[i].path, sizeof files[i].path, "%s", path);
                files[i].data = data;
                break;
            }
    }
    font_t *ft = calloc(1, sizeof *ft);
    if (!ft || !stbtt_InitFont(&ft->info, data, 0)) { free(ft); return NULL; }
    ft->data = data;
    ft->scale = stbtt_ScaleForPixelHeight(&ft->info, px_size);
    int asc, desc, gap;
    stbtt_GetFontVMetrics(&ft->info, &asc, &desc, &gap);
    ft->ascent = (int)(asc * ft->scale + 0.5f);
    ft->height = (int)((asc - desc + gap) * ft->scale + 0.5f);
    return ft;
}

int font_height(font_t *f) { return f->height; }
int font_ascent(font_t *f) { return f->ascent; }

static glyph_t *glyph(font_t *f, uint32_t cp)
{
    unsigned i = (cp * 2654435761u) % GLYPH_SLOTS;
    for (int n = 0; n < GLYPH_SLOTS; n++, i = (i + 1) % GLYPH_SLOTS) {
        glyph_t *g = &f->slots[i];
        if (g->adv && g->cp == cp) return g;
        if (!g->adv) {
            int gi = stbtt_FindGlyphIndex(&f->info, cp);
            if (!gi && cp != ' ') gi = stbtt_FindGlyphIndex(&f->info, '?');
            int adv, lsb;
            stbtt_GetGlyphHMetrics(&f->info, gi, &adv, &lsb);
            g->cp = cp;
            g->adv = (int)(adv * f->scale + 0.5f);
            if (g->adv < 1) g->adv = 1;
            g->bmp = stbtt_GetGlyphBitmap(&f->info, f->scale, f->scale, gi,
                                          &g->w, &g->h, &g->x0, &g->y0);
            return g;
        }
    }
    return &f->slots[0]; /* cache full: unlikely with recipe text */
}

int text_width(font_t *f, const char *s, int n)
{
    const char *end = n < 0 ? s + strlen(s) : s + n;
    int w = 0;
    while (s < end && *s) w += glyph(f, utf8_next(&s))->adv;
    return w;
}

static inline px_t blend(px_t bg, px_t fg, unsigned a)
{
    if (a >= 250) return fg;
    unsigned br = bg >> 11, bgg = (bg >> 5) & 63, bb = bg & 31;
    unsigned fr = fg >> 11, fgg = (fg >> 5) & 63, fb = fg & 31;
    unsigned r = br + (((int)fr - (int)br) * (int)a >> 8);
    unsigned g = bgg + (((int)fgg - (int)bgg) * (int)a >> 8);
    unsigned b = bb + (((int)fb - (int)bb) * (int)a >> 8);
    return (px_t)((r << 11) | (g << 5) | b);
}

int draw_text(canvas_t *c, font_t *f, int x, int y, const char *s, int n, px_t col)
{
    const char *end = n < 0 ? s + strlen(s) : s + n;
    int base = y + f->ascent;
    while (s < end && *s) {
        glyph_t *g = glyph(f, utf8_next(&s));
        if (g->bmp) {
            int gx = x + g->x0, gy = base + g->y0;
            for (int i = 0; i < g->w; i++) {
                int px = gx + i;
                if (px < 0 || px >= c->w) continue;
                px_t *row = c->px + (size_t)PANEL_ROW(c, px) * c->pitch;
                for (int j = 0; j < g->h; j++) {
                    int py = gy + j;
                    if (py < 0 || py >= c->h) continue;
                    unsigned a = g->bmp[j * g->w + i];
                    if (a) row[py] = blend(row[py], col, a);
                }
            }
        }
        x += g->adv;
    }
    return x;
}

int wrap_line(font_t *f, const char *s, int w, const char **next)
{
    const char *p = s, *brk = NULL;
    int x = 0;
    while (*p && *p != '\n') {
        const char *q = p;
        uint32_t cp = utf8_next(&q);
        int adv = glyph(f, cp)->adv;
        if (x + adv > w && p > s) {
            if (brk) {
                const char *n = brk;
                while (*n == ' ') n++;
                *next = n;
                return (int)(brk - s);
            }
            *next = p;
            return (int)(p - s);
        }
        if (cp == ' ') brk = p;
        x += adv;
        p = q;
    }
    *next = *p == '\n' ? p + 1 : p;
    return (int)(p - s);
}
