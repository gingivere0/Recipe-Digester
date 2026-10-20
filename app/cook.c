/* cook: the whole user interface of the Nook recipe OS.
 *
 * Screens: library (list + search), recipe, settings, and Wi-Fi.
 * Rendering only happens when something changed, and the loop sleeps in
 * poll() otherwise, so an idle screen costs almost no CPU.
 */
#include <ctype.h>
#include <dirent.h>
#include <errno.h>
#include <fcntl.h>
#include <poll.h>
#include <signal.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <sys/wait.h>
#include <unistd.h>

#include "gfx.h"
#include "platform.h"

#define VERSION "1.0"

/* ---- look ---- */

#define C_BG      RGB(250, 247, 240)
#define C_TEXT    RGB(34, 30, 26)
#define C_MUTED   RGB(120, 110, 100)
#define C_LINE    RGB(225, 218, 205)
#define C_ACCENT  RGB(190, 80, 40)
#define C_HEAD    RGB(52, 46, 40)
#define C_HEADTXT RGB(250, 247, 240)
#define C_FIELD   RGB(255, 255, 255)
#define C_PRESS   RGB(236, 228, 214)
#define C_KB      RGB(214, 208, 198)
#define C_KEY     RGB(255, 255, 255)
#define C_KEYDARK RGB(178, 170, 158)
#define C_CHECKED RGB(160, 150, 140)

#define W SCREEN_W
#define H SCREEN_H
#define HEADER 76
#define FOOTER 92
#define PAD 28

static const int body_sizes[] = { 26, 30, 35, 41 };
static const char *size_names[] = { "Small", "Medium", "Large", "Extra large" };
static const int timeouts[] = { 5, 15, 30, 60, 0 }; /* minutes on a recipe; 0 = never */
static const char *timeout_names[] = { "5 minutes", "15 minutes", "30 minutes", "1 hour", "Always" };

static const char *FONT_REG = "/usr/share/fonts/dejavu/DejaVuSans.ttf";
static const char *FONT_BOLD = "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf";

static struct {
    font_t *ui, *ui_bold, *small, *title, *key, *back;
    font_t *body[4], *body_bold[4], *body_h1[4], *body_h2[4];
} F;

/* ---- settings ---- */

static char data_dir[200] = "/data/cook";
static struct {
    char source[512];
    int brightness; /* percent */
    int text_size;  /* index into body_sizes */
    int timeout;    /* index into timeouts */
} S = { "", 70, 1, 2 };

static void path_of(char *out, size_t n, const char *name)
{
    snprintf(out, n, "%s/%s", data_dir, name);
}

static void load_settings(void)
{
    char p[256], line[600];
    path_of(p, sizeof p, "settings.txt");
    FILE *f = fopen(p, "r");
    if (!f) return;
    while (fgets(line, sizeof line, f)) {
        line[strcspn(line, "\r\n")] = 0;
        char *eq = strchr(line, '=');
        if (!eq) continue;
        *eq++ = 0;
        if (!strcmp(line, "source")) snprintf(S.source, sizeof S.source, "%s", eq);
        else if (!strcmp(line, "brightness")) S.brightness = atoi(eq);
        else if (!strcmp(line, "text_size")) S.text_size = atoi(eq);
        else if (!strcmp(line, "timeout")) S.timeout = atoi(eq);
    }
    fclose(f);
    if (S.brightness < 5 || S.brightness > 100) S.brightness = 70;
    if (S.text_size < 0 || S.text_size > 3) S.text_size = 1;
    if (S.timeout < 0 || S.timeout > 4) S.timeout = 2;
}

static void save_settings(void)
{
    char p[256], tmp[260];
    path_of(p, sizeof p, "settings.txt");
    snprintf(tmp, sizeof tmp, "%s.new", p);
    FILE *f = fopen(tmp, "w");
    if (!f) return;
    fprintf(f, "source=%s\nbrightness=%d\ntext_size=%d\ntimeout=%d\n",
            S.source, S.brightness, S.text_size, S.timeout);
    fflush(f);
    fsync(fileno(f));
    fclose(f);
    rename(tmp, p);
}

/* ---- recipes ---- */

typedef struct {
    char *file;
    char *title;
    char *subtitle;
    char *text;
    char *lower; /* lowercase copy for searching */
    unsigned char *checked;
    int n_checkable;
} recipe_t;

static recipe_t *recipes;
static int n_recipes;

static char *xstrndup(const char *s, size_t n)
{
    char *d = malloc(n + 1);
    memcpy(d, s, n);
    d[n] = 0;
    return d;
}

static char *read_all(const char *path)
{
    FILE *f = fopen(path, "rb");
    if (!f) return NULL;
    fseek(f, 0, SEEK_END);
    long n = ftell(f);
    fseek(f, 0, SEEK_SET);
    char *s = malloc(n + 1);
    if (s && fread(s, 1, n, f) == (size_t)n) s[n] = 0;
    else { free(s); s = NULL; }
    fclose(f);
    return s;
}

/* strip trailing spaces/CR and leading spaces from [s, e) */
static void trim(const char **s, const char **e)
{
    while (*s < *e && (**s == ' ' || **s == '\t')) (*s)++;
    while (*e > *s && ((*e)[-1] == ' ' || (*e)[-1] == '\r' || (*e)[-1] == '\t')) (*e)--;
}

static int cmp_recipe(const void *a, const void *b)
{
    return strcasecmp(((const recipe_t *)a)->title, ((const recipe_t *)b)->title);
}

static void free_recipes(void)
{
    for (int i = 0; i < n_recipes; i++) {
        recipe_t *r = &recipes[i];
        free(r->file); free(r->title); free(r->subtitle); free(r->text); free(r->lower); free(r->checked);
    }
    free(recipes);
    recipes = NULL;
    n_recipes = 0;
}

static void load_recipes(void)
{
    free_recipes();
    char dir[256];
    path_of(dir, sizeof dir, "recipes");
    DIR *d = opendir(dir);
    if (!d) return;
    int cap = 0;
    struct dirent *de;
    while ((de = readdir(d))) {
        const char *n = de->d_name;
        size_t len = strlen(n);
        if (n[0] == '.' || len < 4) continue;
        if (strcasecmp(n + len - 3, ".md") && strcasecmp(n + len - 4, ".txt")) continue;
        char p[512];
        snprintf(p, sizeof p, "%s/%s", dir, n);
        char *text = read_all(p);
        if (!text) continue;
        if (n_recipes == cap) {
            cap = cap ? cap * 2 : 32;
            recipes = realloc(recipes, cap * sizeof *recipes);
        }
        recipe_t *r = &recipes[n_recipes++];
        memset(r, 0, sizeof *r);
        r->file = strdup(n);
        r->text = text;
        /* title: first "# " line, else the file name; subtitle: first plain line */
        const char *p2 = text;
        while (*p2) {
            const char *e = strchr(p2, '\n');
            if (!e) e = p2 + strlen(p2);
            const char *s = p2, *t = e;
            trim(&s, &t);
            if (!r->title && t - s > 2 && s[0] == '#' && s[1] == ' ') {
                s += 2;
                trim(&s, &t);
                r->title = xstrndup(s, t - s);
            } else if (r->title && !r->subtitle && t > s && s[0] != '#' && s[0] != '-' && s[0] != '*' &&
                       !isdigit((unsigned char)s[0])) {
                r->subtitle = xstrndup(s, t - s);
            } else if (r->title && t - s > 1 && s[0] == '#') {
                break; /* reached the first section */
            }
            p2 = *e ? e + 1 : e;
        }
        if (!r->title) {
            r->title = strdup(n);
            char *dot = strrchr(r->title, '.');
            if (dot) *dot = 0;
            for (char *c = r->title; *c; c++) if (*c == '-' || *c == '_') *c = ' ';
        }
        if (!r->subtitle) r->subtitle = strdup("");
        r->lower = strdup(text);
        for (char *c = r->lower; *c; c++) *c = tolower((unsigned char)*c);
    }
    closedir(d);
    qsort(recipes, n_recipes, sizeof *recipes, cmp_recipe);
}

/* ---- scroll views: content pre-rendered into a tall canvas ---- */

typedef struct {
    canvas_t c;
    int top, height; /* where on screen */
    float pos;       /* scroll offset */
    float vel;       /* px/s while flinging */
} scroll_t;

static int scroll_max(scroll_t *s)
{
    int m = s->c.h - s->height;
    return m > 0 ? m : 0;
}

static void scroll_clamp(scroll_t *s)
{
    if (s->pos < 0) { s->pos = 0; s->vel = 0; }
    if (s->pos > scroll_max(s)) { s->pos = scroll_max(s); s->vel = 0; }
}

static void scroll_alloc(scroll_t *s, int content_h)
{
    if (content_h < 1) content_h = 1;
    if (content_h > 30000) content_h = 30000;
    if (!s->c.px || s->c.h != content_h) {
        canvas_free(&s->c);
        canvas_init(&s->c, W, content_h);
    }
    fill_rect(&s->c, 0, 0, W, content_h, C_BG);
}

static void scroll_draw(canvas_t *scr, scroll_t *s)
{
    scroll_clamp(s);
    int vis = s->c.h < s->height ? s->c.h : s->height;
    blit_rows(scr, s->top, &s->c, (int)s->pos, vis);
    if (vis < s->height) fill_rect(scr, 0, s->top + vis, W, s->height - vis, C_BG);
    /* thin position indicator while there's more to scroll */
    if (scroll_max(s) > 0) {
        int bar = s->height * s->height / s->c.h;
        if (bar < 40) bar = 40;
        int y = s->top + (int)((s->height - bar) * s->pos / scroll_max(s));
        fill_round(scr, W - 8, y, 5, bar, 2, C_LINE);
    }
}

/* ---- app state ---- */

enum { SCR_LIBRARY, SCR_RECIPE, SCR_SETTINGS, SCR_WIFI };
enum { KB_NONE, KB_SEARCH, KB_PASSWORD, KB_SOURCE };

static canvas_t screen;
static int scr = SCR_LIBRARY;
static int dirty = 1;

static scroll_t lib_view, recipe_view, wifi_view;
static int *lib_rows, n_lib_rows; /* recipe indices shown */
static int cur_recipe = -1;

typedef struct { int y0, y1, idx; } hit_t;
static hit_t *recipe_hits;
static int n_recipe_hits;

static char query[128];

/* keyboard */
static int kb = KB_NONE, kb_page, kb_shift, kb_pressed = -1;
static char kb_text[512];
static char kb_prompt[64];

/* modal dialog */
static struct {
    int on;
    char title[64], msg[256];
    char btn[2][24];
    int action[2];
} dlg;
enum { ACT_NONE, ACT_CLOSE, ACT_POWEROFF, ACT_ANDROID, ACT_CANCEL_JOB, ACT_SET_SOURCE };

/* background job (wifi scan/connect, sync) */
enum { JOB_NONE, JOB_SCAN, JOB_CONNECT, JOB_SYNC };
static struct {
    int kind;
    pid_t pid;
    int fd;
    char buf[1024];
    int len;
    char status[200];
    int ok;
} job = { .fd = -1 };

typedef struct { char ssid[64]; int signal; int secure; } net_t;
static net_t nets[40];
static int n_nets;
static char wifi_status[200] = "";
static char wifi_pick[64];
static char saved_ssid[64];

/* brief message near the bottom (brightness changes) */
static char toast[64];
static long long toast_until;

/* power */
static int asleep, dimmed, ignore_power_up;
static long long last_input, power_down_at;
static int battery = -1, charging;
static long long battery_at;

/* touch tracking */
static struct {
    int down, x0, y0, x, y, moved;
    long long t0;
    scroll_t *drag;
    float drag_start;
    int hist_y[6];
    long long hist_t[6];
    int nh;
} touch;

/* ---- layout helpers ---- */

static int in_rect(int x, int y, int rx, int ry, int rw, int rh)
{
    return x >= rx && x < rx + rw && y >= ry && y < ry + rh;
}

/* draw text cut with an ellipsis to fit width w */
static void draw_fit(canvas_t *c, font_t *f, int x, int y, int w, const char *s, px_t col)
{
    int len = strlen(s);
    if (text_width(f, s, len) <= w) { draw_text(c, f, x, y, s, len, col); return; }
    int ell = text_width(f, "…", -1);
    while (len > 0 && text_width(f, s, len) + ell > w) {
        len--;
        while (len > 0 && ((unsigned char)s[len] & 0xc0) == 0x80) len--;
    }
    int nx = draw_text(c, f, x, y, s, len, col);
    draw_text(c, f, nx, y, "…", -1, col);
}

static void draw_center(canvas_t *c, font_t *f, int x, int y, int w, const char *s, px_t col)
{
    draw_text(c, f, x + (w - text_width(f, s, -1)) / 2, y, s, -1, col);
}

static void draw_battery(canvas_t *c, int right, int y)
{
    if (battery < 0) return;
    char s[16];
    snprintf(s, sizeof s, "%d%%", battery);
    int tw = text_width(F.small, s, -1);
    int bx = right - 40;
    draw_text(c, F.small, bx - 10 - tw, y + 4, s, -1, C_HEADTXT);
    frame_rect(c, bx, y + 6, 34, 20, 2, C_HEADTXT);
    fill_rect(c, bx + 34, y + 11, 4, 10, C_HEADTXT);
    int fillw = 30 * battery / 100;
    fill_rect(c, bx + 2, y + 8, fillw, 16, battery <= 15 && charging != 1 ? C_ACCENT : C_HEADTXT);
    if (charging == 1) {
        /* little bolt */
        fill_rect(c, bx + 2, y + 8, 30, 16, C_HEADTXT);
        draw_text(c, F.small, bx + 9, y + 1, "⚡", -1, C_HEAD);
    }
}

static void draw_header(const char *title, int back)
{
    fill_rect(&screen, 0, 0, W, HEADER, C_HEAD);
    int x = PAD;
    if (back) {
        draw_text(&screen, F.back, 16, (HEADER - font_height(F.back)) / 2 - 4, "‹", -1, C_HEADTXT);
        x = 64;
    }
    draw_fit(&screen, F.ui_bold, x, (HEADER - font_height(F.ui_bold)) / 2, W - x - 150, title, C_HEADTXT);
    draw_battery(&screen, W - 18, (HEADER - 32) / 2);
}

static void draw_button(int x, int y, int w, int h, const char *label, int primary)
{
    fill_round(&screen, x, y, w, h, 14, primary ? C_ACCENT : C_HEAD);
    draw_center(&screen, F.ui_bold, x, y + (h - font_height(F.ui_bold)) / 2, w, label, C_HEADTXT);
}

/* ---- library ---- */

#define ROW_H 104
#define SEARCH_Y HEADER
#define SEARCH_H 84

static int lib_visible_bottom(void);

static int matches(recipe_t *r, const char *q)
{
    if (!*q) return 1;
    /* every word in the query must appear somewhere in the recipe */
    char word[64];
    const char *p = q;
    while (*p) {
        while (*p == ' ') p++;
        int n = 0;
        while (*p && *p != ' ' && n < 63) word[n++] = tolower((unsigned char)*p++);
        word[n] = 0;
        if (n && !strstr(r->lower, word)) return 0;
    }
    return 1;
}

static void build_library(void)
{
    free(lib_rows);
    lib_rows = malloc((n_recipes + 1) * sizeof(int));
    n_lib_rows = 0;
    /* title matches first, then matches elsewhere in the text */
    for (int pass = 0; pass < 2; pass++)
        for (int i = 0; i < n_recipes; i++) {
            char lt[256];
            snprintf(lt, sizeof lt, "%s", recipes[i].title);
            for (char *c = lt; *c; c++) *c = tolower((unsigned char)*c);
            int in_title = !*query || strstr(lt, query) != NULL;
            if ((pass == 0) == in_title && matches(&recipes[i], query)) lib_rows[n_lib_rows++] = i;
        }

    lib_view.top = SEARCH_Y + SEARCH_H;
    lib_view.height = lib_visible_bottom() - lib_view.top;
    scroll_alloc(&lib_view, n_lib_rows * ROW_H + 40);
    canvas_t *c = &lib_view.c;
    for (int i = 0; i < n_lib_rows; i++) {
        recipe_t *r = &recipes[lib_rows[i]];
        int y = i * ROW_H;
        draw_fit(c, F.ui_bold, PAD, y + 18, W - 2 * PAD, r->title, C_TEXT);
        draw_fit(c, F.small, PAD, y + 60, W - 2 * PAD, r->subtitle, C_MUTED);
        fill_rect(c, PAD, y + ROW_H - 1, W - 2 * PAD, 1, C_LINE);
    }
    lib_view.pos = 0;
    dirty = 1;
}

static int lib_visible_bottom(void)
{
    extern int kb_height(void);
    return kb ? H - kb_height() : H - FOOTER;
}

static void draw_library(void)
{
    draw_header("Recipes", 0);
    /* search box */
    fill_rect(&screen, 0, SEARCH_Y, W, SEARCH_H, C_BG);
    int active = kb == KB_SEARCH;
    fill_round(&screen, PAD - 8, SEARCH_Y + 14, W - 2 * PAD + 16, SEARCH_H - 24, 14, active ? C_ACCENT : C_LINE);
    fill_round(&screen, PAD - 6, SEARCH_Y + 16, W - 2 * PAD + 12, SEARCH_H - 28, 12, C_FIELD);
    int ty = SEARCH_Y + (SEARCH_H - font_height(F.ui)) / 2 - 3;
    if (*query) {
        int x = draw_text(&screen, F.ui, PAD + 10, ty, query, -1, C_TEXT);
        if (active) fill_rect(&screen, x + 2, ty + 4, 2, font_height(F.ui) - 6, C_ACCENT);
        draw_text(&screen, F.ui, W - PAD - 34, ty, "✕", -1, C_MUTED);
    } else {
        if (active) fill_rect(&screen, PAD + 10, ty + 4, 2, font_height(F.ui) - 6, C_ACCENT);
        draw_text(&screen, F.ui, PAD + 16, ty, "Search recipes", -1, C_MUTED);
    }

    lib_view.height = lib_visible_bottom() - lib_view.top;
    scroll_draw(&screen, &lib_view);
    if (!n_lib_rows) {
        const char *m1 = n_recipes ? "No matches" : "No recipes yet";
        const char *m2 = n_recipes ? "Try a different word." : "Tap Sync to download your recipes.";
        draw_center(&screen, F.ui_bold, 0, lib_view.top + 60, W, m1, C_TEXT);
        draw_center(&screen, F.small, 0, lib_view.top + 110, W, m2, C_MUTED);
    }

    if (!kb) {
        fill_rect(&screen, 0, H - FOOTER, W, FOOTER, C_BG);
        fill_rect(&screen, 0, H - FOOTER, W, 1, C_LINE);
        draw_button(PAD, H - FOOTER + 14, (W - 3 * PAD) / 2, FOOTER - 28, "Sync", 1);
        draw_button(W / 2 + PAD / 2, H - FOOTER + 14, (W - 3 * PAD) / 2, FOOTER - 28, "Settings", 0);
    }
}

/* ---- recipe view ---- */

enum { B_TITLE, B_H2, B_ITEM, B_STEP, B_PARA, B_GAP };

/* lays out (and with draw=1, renders) the current recipe; returns height */
static int layout_recipe(canvas_t *c, int draw)
{
    recipe_t *r = &recipes[cur_recipe];
    int sz = S.text_size;
    font_t *body = F.body[sz], *bold = F.body_bold[sz], *h1 = F.body_h1[sz], *h2 = F.body_h2[sz];
    int lh = font_height(body) * 13 / 10;
    int y = 24, idx = 0, prev_gap = 1;
    int text_w = W - 2 * PAD;

    if (draw) n_recipe_hits = 0;
    const char *p = r->text;
    while (*p) {
        const char *e = strchr(p, '\n');
        if (!e) e = p + strlen(p);
        const char *s = p, *t = e;
        p = *e ? e + 1 : e;
        trim(&s, &t);
        char line[2048];
        int n = t - s < (int)sizeof line - 1 ? (int)(t - s) : (int)sizeof line - 1;
        memcpy(line, s, n);
        line[n] = 0;

        int kind, indent = 0;
        char *txt = line, marker[16] = "";
        if (!n) { if (!prev_gap) y += lh / 2; prev_gap = 1; continue; }
        if (line[0] == '#' && line[1] == ' ') { kind = B_TITLE; txt += 2; }
        else if (line[0] == '#') { kind = B_H2; while (*txt == '#') txt++; while (*txt == ' ') txt++; }
        else if ((line[0] == '-' || line[0] == '*') && line[1] == ' ') { kind = B_ITEM; txt += 2; }
        else if (isdigit((unsigned char)line[0])) {
            char *q = line;
            while (isdigit((unsigned char)*q)) q++;
            if ((*q == '.' || *q == ')') && q[1] == ' ') {
                kind = B_STEP;
                snprintf(marker, sizeof marker, "%.*s", (int)(q - line), line);
                txt = q + 2;
            } else kind = B_PARA;
        } else kind = B_PARA;
        prev_gap = 0;

        font_t *f = body;
        px_t col = C_TEXT;
        int checkable = kind == B_ITEM || kind == B_STEP;
        int checked = checkable && r->checked && idx < r->n_checkable && r->checked[idx];
        if (kind == B_TITLE) { f = h1; y += 8; }
        if (kind == B_H2) { f = h2; col = C_ACCENT; y += lh / 2; }
        if (checkable) { indent = kind == B_STEP ? font_height(bold) * 3 / 2 : font_height(body); }
        if (checked) col = C_CHECKED;

        int line_h = kind == B_TITLE || kind == B_H2 ? font_height(f) * 12 / 10 : lh;
        int y0 = y;
        /* marker */
        if (draw && kind == B_ITEM) {
            int d = font_height(body) / 3;
            int cy = y + font_ascent(body) - font_height(body) / 3;
            if (checked) draw_text(c, bold, PAD, y, "✓", -1, C_ACCENT);
            else fill_round(c, PAD + 4, cy - d / 2, d, d, d / 2, C_ACCENT);
        }
        if (draw && kind == B_STEP) {
            if (checked) draw_text(c, bold, PAD, y, "✓", -1, C_ACCENT);
            else draw_text(c, bold, PAD, y, marker, -1, C_ACCENT);
        }
        const char *q = txt;
        while (*q) {
            const char *next;
            int len = wrap_line(f, q, text_w - indent, &next);
            if (draw) {
                int x1 = draw_text(c, f, PAD + indent, y, q, len, col);
                if (checked)
                    fill_rect(c, PAD + indent, y + font_ascent(f) * 2 / 3, x1 - PAD - indent, 2, C_CHECKED);
            }
            y += line_h;
            if (next == q) break;
            q = next;
        }
        if (kind == B_TITLE) {
            if (draw) fill_rect(c, PAD, y + 4, 80, 4, C_ACCENT);
            y += 24;
        } else if (kind == B_H2) {
            y += 4;
        } else if (checkable) {
            y += lh / 4;
        }
        if (checkable) {
            if (draw) {
                recipe_hits = realloc(recipe_hits, (n_recipe_hits + 1) * sizeof *recipe_hits);
                recipe_hits[n_recipe_hits++] = (hit_t){ y0, y, idx };
            }
            idx++;
        }
    }
    if (!r->checked || r->n_checkable != idx) {
        free(r->checked);
        r->checked = calloc(idx + 1, 1);
        r->n_checkable = idx;
    }
    return y + 120;
}

static void build_recipe(int keep_pos)
{
    float pos = recipe_view.pos;
    canvas_t probe = { 0 };
    int h = layout_recipe(&probe, 0);
    recipe_view.top = HEADER;
    recipe_view.height = H - HEADER;
    scroll_alloc(&recipe_view, h);
    layout_recipe(&recipe_view.c, 1);
    recipe_view.pos = keep_pos ? pos : 0;
    recipe_view.vel = 0;
    dirty = 1;
}

static void draw_recipe(void)
{
    draw_header(recipes[cur_recipe].title, 1);
    /* text size buttons over the battery spot */
    fill_rect(&screen, W - 150, 0, 150, HEADER, C_HEAD);
    draw_text(&screen, F.ui_bold, W - 140, (HEADER - font_height(F.ui_bold)) / 2, "A−", -1, C_HEADTXT);
    draw_text(&screen, F.title, W - 68, (HEADER - font_height(F.title)) / 2, "A+", -1, C_HEADTXT);
    scroll_draw(&screen, &recipe_view);
}

/* ---- settings ---- */

#define SET_ROW 96
enum { SET_WIFI, SET_SOURCE, SET_SYNC, SET_BRIGHT, SET_SIZE, SET_TIMEOUT, SET_BATTERY, SET_POWER, SET_ANDROID, SET_COUNT };

static int android_available(void)
{
    return !access("/rom/u-boot.altboot", F_OK) || plat_sim;
}

static void draw_settings(void)
{
    draw_header("Settings", 1);
    fill_rect(&screen, 0, HEADER, W, H - HEADER, C_BG);
    char val[600];
    for (int i = 0; i < SET_COUNT; i++) {
        if (i == SET_ANDROID && !android_available()) continue;
        int y = HEADER + i * SET_ROW;
        const char *label = "";
        val[0] = 0;
        switch (i) {
        case SET_WIFI: label = "Wi-Fi"; snprintf(val, sizeof val, "%s", *saved_ssid ? saved_ssid : "Not set up"); break;
        case SET_SOURCE: label = "Recipe source"; snprintf(val, sizeof val, "%s", *S.source ? S.source : "Not set"); break;
        case SET_SYNC: label = "Sync recipes now"; snprintf(val, sizeof val, "%d recipes on this Nook", n_recipes); break;
        case SET_BRIGHT: label = "Brightness"; snprintf(val, sizeof val, "%d%%", S.brightness); break;
        case SET_SIZE: label = "Text size"; snprintf(val, sizeof val, "%s", size_names[S.text_size]); break;
        case SET_TIMEOUT: label = "Screen stays on (recipe)"; snprintf(val, sizeof val, "%s", timeout_names[S.timeout]); break;
        case SET_BATTERY: label = "Battery";
            if (battery >= 0) snprintf(val, sizeof val, "%d%%%s", battery,
                                       charging == 1 ? ", charging" : charging == 2 ? ", plugged in but not charging (weak charger)" : "");
            else snprintf(val, sizeof val, "Unknown");
            break;
        case SET_POWER: label = "Power off"; snprintf(val, sizeof val, "Hold the power button for the same thing"); break;
        case SET_ANDROID: label = "Restart into Android"; snprintf(val, sizeof val, "Old system, until it's removed"); break;
        }
        draw_text(&screen, F.ui_bold, PAD, y + 16, label, -1, C_TEXT);
        if (i == SET_BRIGHT) {
            draw_text(&screen, F.small, PAD, y + 58, val, -1, C_MUTED);
            draw_button(W - PAD - 200, y + 18, 90, 64, "−", 0);
            draw_button(W - PAD - 90, y + 18, 90, 64, "+", 0);
        } else {
            draw_fit(&screen, F.small, PAD, y + 58, W - 2 * PAD, val, C_MUTED);
        }
        fill_rect(&screen, PAD, y + SET_ROW - 1, W - 2 * PAD, 1, C_LINE);
    }
    char ver[64];
    snprintf(ver, sizeof ver, "Nook Recipes %s", VERSION);
    draw_center(&screen, F.small, 0, H - 40, W, ver, C_MUTED);
}

/* ---- wifi screen ---- */

#define NET_ROW 92

static void build_wifi(void)
{
    wifi_view.top = HEADER + 70;
    wifi_view.height = H - wifi_view.top;
    scroll_alloc(&wifi_view, n_nets * NET_ROW + 20);
    canvas_t *c = &wifi_view.c;
    for (int i = 0; i < n_nets; i++) {
        int y = i * NET_ROW;
        int bars = nets[i].signal >= -55 ? 4 : nets[i].signal >= -65 ? 3 : nets[i].signal >= -75 ? 2 : 1;
        for (int b = 0; b < 4; b++)
            fill_rect(c, PAD + b * 9, y + 56 - (b + 1) * 9, 6, (b + 1) * 9, b < bars ? C_TEXT : C_LINE);
        int x = PAD + 50;
        draw_fit(c, F.ui_bold, x, y + 26, W - x - PAD - 40, nets[i].ssid,
                 !strcmp(nets[i].ssid, saved_ssid) ? C_ACCENT : C_TEXT);
        if (nets[i].secure) {
            /* small padlock */
            frame_rect(c, W - PAD - 22, y + 28, 14, 14, 3, C_MUTED);
            fill_rect(c, W - PAD - 28, y + 40, 26, 20, C_MUTED);
        }
        fill_rect(c, PAD, y + NET_ROW - 1, W - 2 * PAD, 1, C_LINE);
    }
    dirty = 1;
}

static void draw_wifi(void)
{
    draw_header("Wi-Fi", 1);
    fill_rect(&screen, 0, HEADER, W, 70, C_BG);
    const char *st = *wifi_status ? wifi_status : "Tap your network to connect.";
    draw_fit(&screen, F.small, PAD, HEADER + 22, W - 2 * PAD - 110, st, C_MUTED);
    if (job.kind != JOB_SCAN) draw_text(&screen, F.ui_bold, W - PAD - 92, HEADER + 18, "Rescan", -1, C_ACCENT);
    fill_rect(&screen, 0, HEADER + 69, W, 1, C_LINE);
    scroll_draw(&screen, &wifi_view);
}

/* ---- keyboard ---- */

#define KB_ROW 86
#define KB_FIELD 92

typedef struct { int x, y, w, h; const char *label; int code; } kbkey_t;
enum { KC_SHIFT = 1, KC_BKSP, KC_PAGE, KC_SPACE, KC_DONE, KC_CHAR };

static kbkey_t keys[48];
static int n_keys;

int kb_height(void)
{
    return 4 * KB_ROW + 16 + (kb == KB_SEARCH ? 0 : KB_FIELD);
}

static void add_key(int x, int y, int w, const char *label, int code)
{
    keys[n_keys++] = (kbkey_t){ x, y, w, KB_ROW, label, code };
}

static void layout_keys(void)
{
    static const char *pages[3][3] = {
        { "qwertyuiop", "asdfghjkl", "zxcvbnm" },
        { "1234567890", "-/:;()$&@\"", ".,?!'*%" },
        { "[]{}#^+=<>", "_\\|~`'\"&@$", ".,?!;:-" },
    };
    static char single[48][2];
    n_keys = 0;
    int top = H - 4 * KB_ROW - 8;
    for (int r = 0; r < 3; r++) {
        const char *row = pages[kb_page][r];
        int n = strlen(row);
        int kw = 60, x = (W - n * kw) / 2;
        if (r == 2) {
            x = 90;
            add_key(0, top + r * KB_ROW, 90, kb_page == 0 ? "⇧" : kb_page == 1 ? "#+=" : "123", KC_SHIFT);
        }
        for (int i = 0; i < n; i++) {
            char *s = single[n_keys];
            s[0] = row[i];
            if (kb_page == 0 && kb_shift) s[0] = toupper((unsigned char)s[0]);
            s[1] = 0;
            add_key(x + i * kw, top + r * KB_ROW, kw, s, KC_CHAR);
        }
        if (r == 2) add_key(510, top + r * KB_ROW, 90, "⌫", KC_BKSP);
    }
    int y = top + 3 * KB_ROW;
    add_key(0, y, 120, kb_page ? "abc" : "?123", KC_PAGE);
    add_key(120, y, 300, "space", KC_SPACE);
    add_key(420, y, 60, ".", KC_CHAR);
    add_key(480, y, 120, kb == KB_SEARCH ? "Done" : "OK", KC_DONE);
}

static void draw_keyboard(void)
{
    int top = H - kb_height();
    fill_rect(&screen, 0, top, W, H - top, C_KB);
    if (kb != KB_SEARCH) {
        fill_rect(&screen, 0, top, W, KB_FIELD, C_BG);
        fill_rect(&screen, 0, top, W, 1, C_LINE);
        draw_text(&screen, F.small, PAD, top + 8, kb_prompt, -1, C_MUTED);
        /* show the end of long text */
        const char *t = kb_text;
        while (text_width(F.ui, t, -1) > W - 2 * PAD - 10 && *t) t++;
        int x = draw_text(&screen, F.ui, PAD, top + 42, t, -1, C_TEXT);
        fill_rect(&screen, x + 2, top + 46, 2, font_height(F.ui) - 6, C_ACCENT);
    }
    layout_keys();
    for (int i = 0; i < n_keys; i++) {
        kbkey_t *k = &keys[i];
        int special = k->code != KC_CHAR && k->code != KC_SPACE;
        px_t bg = i == kb_pressed ? C_KEYDARK : k->code == KC_DONE ? C_ACCENT : special ? C_KEYDARK : C_KEY;
        if (k->code == KC_SHIFT && kb_shift && kb_page == 0) bg = C_HEAD;
        fill_round(&screen, k->x + 4, k->y + 6, k->w - 8, k->h - 12, 10, bg);
        px_t fg = k->code == KC_DONE || (k->code == KC_SHIFT && kb_shift && kb_page == 0) ? C_HEADTXT : C_TEXT;
        font_t *f = k->code == KC_CHAR ? F.key : F.small;
        draw_center(&screen, f, k->x, k->y + (k->h - font_height(f)) / 2, k->w, k->label, fg);
    }
}

static void kb_open(int target, const char *prompt, const char *initial)
{
    kb = target;
    kb_page = 0;
    kb_shift = 0;
    kb_pressed = -1;
    snprintf(kb_prompt, sizeof kb_prompt, "%s", prompt);
    snprintf(kb_text, sizeof kb_text, "%s", initial);
    if (target == KB_SEARCH) build_library();
    dirty = 1;
}

/* ---- jobs ---- */

static void start_job(int kind, char *const argv[], const char *status)
{
    if (job.kind) return;
    int p[2];
    if (pipe(p)) return;
    pid_t pid = fork();
    if (pid == 0) {
        setsid();
        dup2(p[1], 1);
        dup2(p[1], 2);
        close(p[0]);
        close(p[1]);
        execvp(argv[0], argv);
        printf("ERR cannot run %s\n", argv[0]);
        _exit(127);
    }
    close(p[1]);
    if (pid < 0) { close(p[0]); return; }
    fcntl(p[0], F_SETFL, O_NONBLOCK);
    fcntl(p[0], F_SETFD, FD_CLOEXEC);
    job.kind = kind;
    job.pid = pid;
    job.fd = p[0];
    job.len = 0;
    job.ok = 0;
    snprintf(job.status, sizeof job.status, "%s", status);
    plat_wakelock(1);
    dirty = 1;
}

static void read_saved_ssid(void)
{
    char p[256];
    path_of(p, sizeof p, "wpa_supplicant.conf");
    saved_ssid[0] = 0;
    char *conf = read_all(p);
    if (!conf) return;
    char *s = strstr(conf, "ssid=\"");
    if (s) {
        s += 6;
        char *e = strchr(s, '"');
        if (e) snprintf(saved_ssid, sizeof saved_ssid, "%.*s", (int)(e - s), s);
    }
    free(conf);
}

static void job_line(char *line)
{
    if (!strncmp(line, "MSG ", 4)) {
        snprintf(job.status, sizeof job.status, "%s", line + 4);
        if (job.kind != JOB_SYNC) snprintf(wifi_status, sizeof wifi_status, "%s", line + 4);
    } else if (!strncmp(line, "ERR ", 4)) {
        snprintf(job.status, sizeof job.status, "%s", line + 4);
        snprintf(wifi_status, sizeof wifi_status, "%s", line + 4);
        job.ok = -1;
    } else if (!strncmp(line, "DONE", 4)) {
        job.ok = 1;
        if (line[4] == ' ') snprintf(job.status, sizeof job.status, "%s", line + 5);
    } else if (!strncmp(line, "NET ", 4) && n_nets < 40) {
        /* NET <signal> <secure 0/1> <ssid> */
        net_t *n = &nets[n_nets];
        char *a = line + 4, *b = strchr(a, ' ');
        if (!b) return;
        char *c = strchr(b + 1, ' ');
        if (!c || !c[1]) return;
        n->signal = atoi(a);
        n->secure = atoi(b + 1);
        snprintf(n->ssid, sizeof n->ssid, "%s", c + 1);
        for (int i = 0; i < n_nets; i++)
            if (!strcmp(nets[i].ssid, n->ssid)) return; /* duplicate access point */
        n_nets++;
    }
    dirty = 1;
}

static void show_dialog(const char *title, const char *msg, const char *b0, int a0, const char *b1, int a1)
{
    dlg.on = 1;
    snprintf(dlg.title, sizeof dlg.title, "%s", title);
    snprintf(dlg.msg, sizeof dlg.msg, "%s", msg);
    snprintf(dlg.btn[0], sizeof dlg.btn[0], "%s", b0 ? b0 : "");
    snprintf(dlg.btn[1], sizeof dlg.btn[1], "%s", b1 ? b1 : "");
    dlg.action[0] = a0;
    dlg.action[1] = a1;
    dirty = 1;
}

static void job_finished(void)
{
    int kind = job.kind;
    close(job.fd);
    job.fd = -1;
    job.kind = JOB_NONE;
    waitpid(job.pid, NULL, 0);
    plat_wakelock(0);
    if (kind == JOB_SCAN) {
        build_wifi();
        if (job.ok >= 0) snprintf(wifi_status, sizeof wifi_status, n_nets ? "Tap your network to connect." : "No networks found. Tap Rescan.");
    } else if (kind == JOB_CONNECT) {
        read_saved_ssid();
        if (job.ok > 0) {
            snprintf(wifi_status, sizeof wifi_status, "Connected. Wi-Fi turns off until you sync.");
            build_wifi();
        }
    } else if (kind == JOB_SYNC) {
        load_recipes();
        query[0] = 0;
        build_library();
        if (job.ok > 0) show_dialog("Sync finished", job.status, "OK", ACT_CLOSE, NULL, 0);
        else show_dialog("Sync failed", job.status, "OK", ACT_CLOSE, NULL, 0);
    }
    dirty = 1;
}

static void poll_job(void)
{
    if (job.fd < 0) return;
    for (;;) {
        ssize_t n = read(job.fd, job.buf + job.len, sizeof job.buf - 1 - job.len);
        if (n > 0) {
            job.len += n;
            job.buf[job.len] = 0;
            char *nl;
            while ((nl = strchr(job.buf, '\n'))) {
                *nl = 0;
                job_line(job.buf);
                memmove(job.buf, nl + 1, job.len - (nl + 1 - job.buf) + 1);
                job.len -= nl + 1 - job.buf;
            }
            if (job.len == sizeof job.buf - 1) job.len = 0; /* runaway line */
        } else if (n == 0) {
            job_finished();
            return;
        } else {
            return; /* EAGAIN */
        }
    }
}

static void cancel_job(void)
{
    if (!job.kind) return;
    kill(-job.pid, SIGTERM);
    snprintf(job.status, sizeof job.status, "Canceling…");
    /* let the helper clean up (it turns Wi-Fi off on SIGTERM) */
    char *argv[] = { "wifi", "down", NULL };
    if (fork() == 0) { execvp(argv[0], argv); _exit(1); }
}

static void start_sync(void)
{
    if (!*S.source) {
        show_dialog("No recipe source", "Set the web address of your recipe list first.",
                    "Set it", ACT_SET_SOURCE, "Cancel", ACT_CLOSE);
        return;
    }
    char *argv[] = { "cook-sync", S.source, data_dir, NULL };
    start_job(JOB_SYNC, argv, "Starting Wi-Fi…");
}

static void start_scan(void)
{
    n_nets = 0;
    build_wifi();
    snprintf(wifi_status, sizeof wifi_status, "Looking for networks…");
    char *argv[] = { "wifi", "scan", NULL };
    start_job(JOB_SCAN, argv, "Looking for networks…");
}

/* ---- dialog + busy overlay ---- */

static void draw_overlay_box(int h, int *bx, int *by, int *bw)
{
    /* darken what's behind by drawing a scrim of horizontal stripes is too slow;
       a solid frame reads well and is cheap */
    *bw = W - 2 * 40;
    *bx = 40;
    *by = (H - h) / 2;
    fill_round(&screen, *bx - 3, *by - 3, *bw + 6, h + 6, 22, C_HEAD);
    fill_round(&screen, *bx, *by, *bw, h, 20, C_BG);
}

static void wrap_draw(font_t *f, int x, int y, int w, const char *s, px_t col, int max_lines)
{
    for (int i = 0; i < max_lines && *s; i++) {
        const char *next;
        int len = wrap_line(f, s, w, &next);
        draw_text(&screen, f, x, y, s, len, col);
        y += font_height(f) + 6;
        if (next == s) break;
        s = next;
    }
}

#define DLG_H 330

static void draw_dialog(void)
{
    int bx, by, bw;
    draw_overlay_box(DLG_H, &bx, &by, &bw);
    draw_text(&screen, F.ui_bold, bx + 30, by + 30, dlg.title, -1, C_TEXT);
    wrap_draw(F.small, bx + 30, by + 86, bw - 60, dlg.msg, C_MUTED, 4);
    int nb = dlg.btn[1][0] ? 2 : 1;
    int btw = (bw - 60 - (nb - 1) * 20) / nb;
    for (int i = 0; i < nb; i++)
        draw_button(bx + 30 + i * (btw + 20), by + DLG_H - 100, btw, 72, dlg.btn[i], i == 0);
}

static void draw_busy(void)
{
    int bx, by, bw;
    draw_overlay_box(DLG_H, &bx, &by, &bw);
    const char *title = job.kind == JOB_SYNC ? "Syncing recipes" : "Connecting";
    draw_text(&screen, F.ui_bold, bx + 30, by + 30, title, -1, C_TEXT);
    wrap_draw(F.small, bx + 30, by + 86, bw - 60, job.status, C_MUTED, 4);
    draw_button(bx + 30, by + DLG_H - 100, bw - 60, 72, "Cancel", 0);
}

/* ---- render ---- */

static void render(void)
{
    switch (scr) {
    case SCR_LIBRARY: draw_library(); break;
    case SCR_RECIPE: draw_recipe(); break;
    case SCR_SETTINGS: draw_settings(); break;
    case SCR_WIFI: draw_wifi(); break;
    }
    if (kb) draw_keyboard();
    if (job.kind == JOB_SYNC || job.kind == JOB_CONNECT) draw_busy();
    else if (dlg.on) draw_dialog();
    if (toast_until) {
        int tw = text_width(F.ui_bold, toast, -1) + 60;
        fill_round(&screen, (W - tw) / 2, H - 200, tw, 70, 35, C_HEAD);
        draw_center(&screen, F.ui_bold, 0, H - 200 + (70 - font_height(F.ui_bold)) / 2, W, toast, C_HEADTXT);
    }
    plat_present(&screen);
    dirty = 0;
}

/* ---- actions ---- */

static void go(int s)
{
    scr = s;
    kb = KB_NONE;
    dirty = 1;
    if (s == SCR_WIFI) start_scan();
}

static void open_recipe(int i)
{
    cur_recipe = i;
    scr = SCR_RECIPE;
    kb = KB_NONE;
    build_recipe(0);
}

static void do_action(int a)
{
    dlg.on = 0;
    dirty = 1;
    switch (a) {
    case ACT_POWEROFF:
        fill_rect(&screen, 0, 0, W, H, C_HEAD);
        draw_center(&screen, F.ui_bold, 0, H / 2 - 20, W, "Powering off…", C_HEADTXT);
        plat_present(&screen);
        sync();
        if (!plat_sim) execlp("poweroff", "poweroff", NULL);
        break;
    case ACT_ANDROID:
        unlink("/rom/u-boot.altboot");
        sync();
        if (!plat_sim) execlp("reboot", "reboot", NULL);
        break;
    case ACT_SET_SOURCE:
        go(SCR_SETTINGS);
        kb_open(KB_SOURCE, "Web address of your recipe list", *S.source ? S.source : "https://");
        break;
    }
}

static void kb_commit(void)
{
    int target = kb;
    kb = KB_NONE;
    dirty = 1;
    if (target == KB_SEARCH) {
        build_library();
    } else if (target == KB_SOURCE) {
        char *t = kb_text;
        while (*t == ' ') t++;
        snprintf(S.source, sizeof S.source, "%s", t);
        save_settings();
    } else if (target == KB_PASSWORD) {
        setenv("WIFI_SSID", wifi_pick, 1);
        setenv("WIFI_PSK", kb_text, 1);
        char cfg[256];
        path_of(cfg, sizeof cfg, "wpa_supplicant.conf");
        char *argv[] = { "wifi", "connect", cfg, NULL };
        start_job(JOB_CONNECT, argv, "Connecting…");
        unsetenv("WIFI_PSK");
        memset(kb_text, 0, sizeof kb_text);
    }
}

static void kb_key(kbkey_t *k)
{
    size_t len = strlen(kb == KB_SEARCH ? query : kb_text);
    char *buf = kb == KB_SEARCH ? query : kb_text;
    size_t cap = kb == KB_SEARCH ? sizeof query : sizeof kb_text;
    switch (k->code) {
    case KC_SHIFT:
        if (kb_page == 0) kb_shift = !kb_shift;
        else kb_page = kb_page == 1 ? 2 : 1;
        break;
    case KC_PAGE: kb_page = kb_page ? 0 : 1; kb_shift = 0; break;
    case KC_BKSP:
        while (len > 0 && ((unsigned char)buf[len - 1] & 0xc0) == 0x80) len--;
        if (len > 0) buf[len - 1] = 0;
        break;
    case KC_SPACE: if (len + 1 < cap) { buf[len] = ' '; buf[len + 1] = 0; } break;
    case KC_DONE: kb_commit(); return;
    case KC_CHAR:
        if (len + 1 < cap) { buf[len] = k->label[0]; buf[len + 1] = 0; }
        if (kb_shift && kb_page == 0) kb_shift = 0;
        break;
    }
    if (kb == KB_SEARCH && (k->code == KC_CHAR || k->code == KC_SPACE || k->code == KC_BKSP)) {
        /* search is case-insensitive */
        for (char *c = query; *c; c++) *c = tolower((unsigned char)*c);
        build_library();
    }
    dirty = 1;
}

static int key_at(int x, int y)
{
    layout_keys();
    for (int i = 0; i < n_keys; i++)
        if (in_rect(x, y, keys[i].x, keys[i].y, keys[i].w, keys[i].h)) return i;
    return -1;
}

static void tap(int x, int y)
{
    if (job.kind == JOB_SYNC || job.kind == JOB_CONNECT) {
        int bx = 40, by = (H - DLG_H) / 2;
        if (in_rect(x, y, bx, by + DLG_H - 110, W - 80, 100)) cancel_job();
        return;
    }
    if (dlg.on) {
        int bx = 40, by = (H - DLG_H) / 2, bw = W - 80;
        int nb = dlg.btn[1][0] ? 2 : 1;
        int btw = (bw - 60 - (nb - 1) * 20) / nb;
        for (int i = 0; i < nb; i++)
            if (in_rect(x, y, bx + 30 + i * (btw + 20), by + DLG_H - 110, btw, 92)) do_action(dlg.action[i]);
        return;
    }
    if (kb) {
        int top = H - kb_height();
        if (y >= top) {
            int k = key_at(x, y);
            if (k >= 0) kb_key(&keys[k]);
            return;
        }
        /* tapping above the keyboard closes it (except on the search results) */
        if (kb != KB_SEARCH) { kb = KB_NONE; dirty = 1; return; }
    }
    int back = in_rect(x, y, 0, 0, 120, HEADER);

    switch (scr) {
    case SCR_LIBRARY:
        if (in_rect(x, y, 0, SEARCH_Y, W, SEARCH_H)) {
            if (*query && x > W - PAD - 60) { query[0] = 0; build_library(); }
            else if (!kb) kb_open(KB_SEARCH, "", "");
            return;
        }
        if (!kb && y >= H - FOOTER) {
            if (x < W / 2) start_sync();
            else go(SCR_SETTINGS);
            return;
        }
        if (y >= lib_view.top && y < lib_view.top + lib_view.height) {
            int row = (int)(y - lib_view.top + lib_view.pos) / ROW_H;
            if (row >= 0 && row < n_lib_rows) open_recipe(lib_rows[row]);
        }
        break;
    case SCR_RECIPE:
        if (back) { go(SCR_LIBRARY); return; }
        if (y < HEADER && x >= W - 150) {
            if (x < W - 75) { if (S.text_size > 0) S.text_size--; }
            else if (S.text_size < 3) S.text_size++;
            save_settings();
            build_recipe(0);
            return;
        }
        if (y >= HEADER) {
            int cy = (int)(y - HEADER + recipe_view.pos);
            recipe_t *r = &recipes[cur_recipe];
            for (int i = 0; i < n_recipe_hits; i++)
                if (cy >= recipe_hits[i].y0 && cy < recipe_hits[i].y1 && recipe_hits[i].idx < r->n_checkable) {
                    r->checked[recipe_hits[i].idx] ^= 1;
                    build_recipe(1);
                    return;
                }
        }
        break;
    case SCR_SETTINGS: {
        if (back) { go(SCR_LIBRARY); return; }
        int row = (y - HEADER) / SET_ROW;
        if (y < HEADER || row >= SET_COUNT) return;
        switch (row) {
        case SET_WIFI: go(SCR_WIFI); break;
        case SET_SOURCE: kb_open(KB_SOURCE, "Web address of your recipe list", *S.source ? S.source : "https://"); break;
        case SET_SYNC: go(SCR_LIBRARY); start_sync(); break;
        case SET_BRIGHT:
            if (x >= W - PAD - 200 && x < W - PAD - 100) S.brightness -= 10;
            else if (x >= W - PAD - 100) S.brightness += 10;
            if (S.brightness < 5) S.brightness = 5;
            if (S.brightness > 100) S.brightness = 100;
            plat_backlight(S.brightness);
            save_settings();
            break;
        case SET_SIZE: S.text_size = (S.text_size + 1) % 4; save_settings(); break;
        case SET_TIMEOUT: S.timeout = (S.timeout + 1) % 5; save_settings(); break;
        case SET_POWER: show_dialog("Power off?", "Press the power button to turn it back on.", "Power off", ACT_POWEROFF, "Cancel", ACT_CLOSE); break;
        case SET_ANDROID:
            if (android_available())
                show_dialog("Restart into Android?", "The Nook will boot the old system once. Restart again to come back here.",
                            "Restart", ACT_ANDROID, "Cancel", ACT_CLOSE);
            break;
        }
        dirty = 1;
        break;
    }
    case SCR_WIFI:
        if (back) { if (!job.kind) go(SCR_SETTINGS); return; }
        if (y < HEADER + 70 && x > W - PAD - 120 && !job.kind) { start_scan(); return; }
        if (y >= wifi_view.top && !job.kind) {
            int row = (int)(y - wifi_view.top + wifi_view.pos) / NET_ROW;
            if (row >= 0 && row < n_nets) {
                snprintf(wifi_pick, sizeof wifi_pick, "%s", nets[row].ssid);
                char prompt[96];
                snprintf(prompt, sizeof prompt, "Password for %s", wifi_pick);
                kb_open(KB_PASSWORD, prompt, "");
            }
        }
        break;
    }
}

static scroll_t *view_at(int y)
{
    scroll_t *v = scr == SCR_LIBRARY ? &lib_view : scr == SCR_RECIPE ? &recipe_view : scr == SCR_WIFI ? &wifi_view : NULL;
    if (v && y >= v->top && y < v->top + v->height) return v;
    return NULL;
}

static scroll_t *current_view(void)
{
    return scr == SCR_LIBRARY ? &lib_view : scr == SCR_RECIPE ? &recipe_view : scr == SCR_WIFI ? &wifi_view : NULL;
}

/* ---- input dispatch ---- */

static int idle_limit_ms(void)
{
    if (job.kind) return 0; /* never sleep mid-sync */
    if (scr == SCR_RECIPE) return timeouts[S.timeout] * 60000;
    return 3 * 60000;
}

static void go_to_sleep(void)
{
    if (job.kind) return;
    asleep = 1;
    dimmed = 0;
    kb = KB_NONE;
    plat_sleep();
}

static void wake_up(void)
{
    plat_wake();
    asleep = 0;
    dimmed = 0;
    last_input = now_ms();
    battery_at = 0;
    dirty = 1;
    render();
    plat_backlight(S.brightness);
}

static void handle_event(event_t *e)
{
    long long t = now_ms();
    if (e->type == EV_REDRAW) { if (!asleep) dirty = 1; return; }
    if (asleep) {
        if (e->type == EV_KEY_DOWN && (e->key == K_POWER || e->key == K_HOME)) {
            ignore_power_up = e->key == K_POWER;
            wake_up();
        }
        return;
    }
    last_input = t;
    if (dimmed) { dimmed = 0; plat_backlight(S.brightness); }

    switch (e->type) {
    case EV_KEY_DOWN:
        if (e->key == K_POWER) power_down_at = t;
        else if (e->key == K_HOME) {
            if (dlg.on) dlg.on = 0;
            else if (kb) kb = KB_NONE;
            else if (scr != SCR_LIBRARY && !job.kind) go(SCR_LIBRARY);
            else if (*query) { query[0] = 0; build_library(); }
            dirty = 1;
        } else if (e->key == K_VOLDOWN || e->key == K_VOLUP) {
            S.brightness += e->key == K_VOLUP ? 10 : -10;
            if (S.brightness < 5) S.brightness = 5;
            if (S.brightness > 100) S.brightness = 100;
            plat_backlight(S.brightness);
            save_settings();
            snprintf(toast, sizeof toast, "Brightness %d%%", S.brightness);
            toast_until = t + 1500;
            dirty = 1;
        }
        break;
    case EV_KEY_UP:
        if (e->key == K_POWER) {
            if (ignore_power_up) { ignore_power_up = 0; break; }
            if (power_down_at && t - power_down_at >= 1200)
                show_dialog("Power off?", "Press the power button to turn it back on.", "Power off", ACT_POWEROFF, "Cancel", ACT_CLOSE);
            else
                go_to_sleep();
            power_down_at = 0;
        }
        break;
    case EV_TOUCH_DOWN:
        touch.down = 1;
        touch.x0 = touch.x = e->x;
        touch.y0 = touch.y = e->y;
        touch.moved = 0;
        touch.t0 = t;
        touch.nh = 0;
        touch.drag = (dlg.on || (job.kind == JOB_SYNC || job.kind == JOB_CONNECT)) ? NULL : view_at(e->y);
        if (kb && e->y >= H - kb_height()) touch.drag = NULL;
        if (touch.drag) { touch.drag->vel = 0; touch.drag_start = touch.drag->pos; }
        if (kb && e->y >= H - kb_height()) { kb_pressed = key_at(e->x, e->y); dirty = 1; }
        /* fallthrough */
    case EV_TOUCH_MOVE:
        touch.x = e->x;
        touch.y = e->y;
        if (abs(touch.y - touch.y0) > 16 || abs(touch.x - touch.x0) > 16) touch.moved = 1;
        if (touch.drag && touch.moved) {
            touch.drag->pos = touch.drag_start - (touch.y - touch.y0);
            scroll_clamp(touch.drag);
            dirty = 1;
        }
        if (touch.nh == 6) { memmove(touch.hist_y, touch.hist_y + 1, 5 * sizeof(int)); memmove(touch.hist_t, touch.hist_t + 1, 5 * sizeof(long long)); touch.nh = 5; }
        touch.hist_y[touch.nh] = e->y;
        touch.hist_t[touch.nh] = t;
        touch.nh++;
        break;
    case EV_TOUCH_UP:
        touch.down = 0;
        if (kb_pressed >= 0) { kb_pressed = -1; dirty = 1; }
        if (!touch.moved) {
            tap(touch.x0, touch.y0);
        } else if (touch.drag && touch.nh >= 2) {
            /* fling with the speed of the last ~100 ms */
            int i = 0;
            while (i < touch.nh - 2 && t - touch.hist_t[i] > 100) i++;
            long long dt = touch.hist_t[touch.nh - 1] - touch.hist_t[i];
            if (dt > 0 && t - touch.hist_t[touch.nh - 1] < 80) {
                float v = -(touch.hist_y[touch.nh - 1] - touch.hist_y[i]) * 1000.0f / dt;
                if (v > 6000) v = 6000;
                if (v < -6000) v = -6000;
                touch.drag->vel = v;
            }
        }
        touch.drag = NULL;
        break;
    }
}

/* advance flings; returns 1 while something is still moving */
static int animate(long long dt_ms)
{
    int moving = 0;
    scroll_t *views[] = { &lib_view, &recipe_view, &wifi_view };
    for (int i = 0; i < 3; i++) {
        scroll_t *v = views[i];
        if (v->vel == 0) continue;
        v->pos += v->vel * dt_ms / 1000.0f;
        float decay = 1.0f - 2.8f * dt_ms / 1000.0f; /* friction */
        if (decay < 0) decay = 0;
        v->vel *= decay;
        if (v->vel > -25 && v->vel < 25) v->vel = 0;
        scroll_clamp(v);
        if (v->vel != 0) moving = 1;
        if (v == current_view()) dirty = 1;
    }
    return moving;
}

/* ---- main ---- */

static int load_fonts(void)
{
    F.ui = font_load(FONT_REG, 28);
    F.ui_bold = font_load(FONT_BOLD, 28);
    F.small = font_load(FONT_REG, 22);
    F.title = font_load(FONT_BOLD, 40);
    F.key = font_load(FONT_REG, 32);
    F.back = font_load(FONT_BOLD, 64);
    for (int i = 0; i < 4; i++) {
        F.body[i] = font_load(FONT_REG, body_sizes[i]);
        F.body_bold[i] = font_load(FONT_BOLD, body_sizes[i]);
        F.body_h1[i] = font_load(FONT_BOLD, body_sizes[i] * 3 / 2);
        F.body_h2[i] = font_load(FONT_BOLD, body_sizes[i] * 6 / 5);
    }
    return F.ui && F.ui_bold && F.body[3] ? 0 : -1;
}

int main(int argc, char **argv)
{
    if (argc > 1 && !strcmp(argv[1], "--home-held")) {
        canvas_t dummy;
        (void)dummy;
        return plat_key_held(K_HOME) ? 0 : 1;
    }
    signal(SIGPIPE, SIG_IGN);
    const char *dd = getenv("COOK_DATA");
    if (dd) snprintf(data_dir, sizeof data_dir, "%s", dd);
    const char *fd = getenv("COOK_FONTS");
    static char reg[256], bold[256];
    if (fd) {
        snprintf(reg, sizeof reg, "%s/DejaVuSans.ttf", fd);
        snprintf(bold, sizeof bold, "%s/DejaVuSans-Bold.ttf", fd);
        FONT_REG = reg;
        FONT_BOLD = bold;
    }

    if (plat_init(&screen)) { fprintf(stderr, "cook: no framebuffer\n"); return 1; }
    if (load_fonts()) { fprintf(stderr, "cook: fonts missing\n"); return 1; }
    mkdir(data_dir, 0755);
    load_settings();
    load_recipes();
    read_saved_ssid();
    build_library();
    battery = plat_battery(&charging);
    battery_at = now_ms();
    last_input = now_ms();
    render();
    plat_backlight(S.brightness);

    if (plat_sim) {
        event_t e;
        while (plat_sim_step(&e, &screen)) {
            handle_event(&e);
            while (job.kind) { usleep(10000); poll_job(); }
            /* settle flings instantly in the simulator */
            for (int i = 0; i < 200 && animate(16); i++) ;
            if (dirty) render();
        }
        return 0;
    }

    long long last_frame = now_ms();
    for (;;) {
        struct pollfd pfd[10];
        int fds[8];
        int n = plat_input_fds(fds, 8);
        for (int i = 0; i < n; i++) pfd[i] = (struct pollfd){ fds[i], POLLIN, 0 };
        int njob = -1;
        if (job.fd >= 0) { njob = n; pfd[n++] = (struct pollfd){ job.fd, POLLIN, 0 }; }

        long long t = now_ms();
        int moving = !asleep && (lib_view.vel || recipe_view.vel || wifi_view.vel);
        int timeout = -1;
        if (moving || dirty) timeout = 0;
        else if (!asleep) {
            int lim = idle_limit_ms();
            long long until = 60000 - (t - battery_at); /* battery refresh */
            if (lim) {
                long long idle = t - last_input;
                long long to_dim = lim - 20000 - idle, to_sleep = lim - idle;
                long long next = dimmed ? to_sleep : to_dim;
                if (next < until) until = next;
            }
            if (power_down_at) until = until < 1300 ? until : 1300;
            if (toast_until && toast_until - t < until) until = toast_until - t;
            timeout = until < 0 ? 0 : (int)until;
        }
        if (moving) timeout = 16 - (int)(t - last_frame) > 0 ? 16 - (int)(t - last_frame) : 0;

        poll(pfd, n, timeout);
        t = now_ms();

        event_t e;
        while (plat_next_event(&e)) handle_event(&e);
        if (njob >= 0) poll_job();
        else if (job.fd >= 0) poll_job();

        if (asleep) continue;

        /* long-press power: show the dialog without waiting for release */
        if (power_down_at && t - power_down_at >= 1200) {
            power_down_at = 0;
            ignore_power_up = 1;
            show_dialog("Power off?", "Press the power button to turn it back on.", "Power off", ACT_POWEROFF, "Cancel", ACT_CLOSE);
        }

        if (t - battery_at >= 60000) {
            int b = plat_battery(&charging);
            if (b != battery) dirty = 1;
            battery = b;
            battery_at = t;
        }

        int lim = idle_limit_ms();
        if (lim && !touch.down) {
            long long idle = t - last_input;
            if (idle >= lim) { go_to_sleep(); continue; }
            if (idle >= lim - 20000 && !dimmed) { dimmed = 1; plat_backlight(S.brightness / 4 + 1); }
        }

        if (toast_until && t >= toast_until) { toast_until = 0; dirty = 1; }
        animate(t - last_frame);
        if (dirty) render();
        last_frame = t;
    }
}
