#include "platform.h"

#include <dirent.h>
#include <errno.h>
#include <fcntl.h>
#include <linux/fb.h>
#include <linux/input.h>
#include <pthread.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/mman.h>
#include <time.h>
#include <unistd.h>

int plat_sim;

static int fb_fd = -1;
static unsigned char *fb_mem;
static struct fb_var_screeninfo vinfo;
static struct fb_fix_screeninfo finfo;
static int fb_pages = 1, fb_page;

#define MAX_INPUT 8
static int in_fds[MAX_INPUT];
static int in_touch[MAX_INPUT];
static int n_in;

/* touch calibration: raw range and orientation fixes */
static int touch_max_x = SCREEN_W, touch_max_y = SCREEN_H;
static int touch_flip_x, touch_flip_y, touch_swap;
static int touch_initial_slot;

/* Android-style screen handshake: the kernel waits for us to confirm we
 * stopped drawing before it turns the display off */
static int fbwake_pipe[2] = { -1, -1 };

static void *fb_handshake(void *arg)
{
    char c;
    for (;;) {
        int fd = open("/sys/power/wait_for_fb_sleep", O_RDONLY);
        if (fd < 0) return NULL;
        read(fd, &c, 1); /* blocks until the screen is going off */
        close(fd);
        fd = open("/sys/power/wait_for_fb_wake", O_RDONLY);
        if (fd < 0) return NULL;
        read(fd, &c, 1); /* acknowledges, then blocks until it is back on */
        close(fd);
        write(fbwake_pipe[1], "w", 1);
    }
}

long long now_ms(void)
{
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return ts.tv_sec * 1000LL + ts.tv_nsec / 1000000;
}

static int write_file(const char *path, const char *s)
{
    int fd = open(path, O_WRONLY);
    if (fd < 0) return -1;
    int r = write(fd, s, strlen(s));
    close(fd);
    return r < 0 ? -1 : 0;
}

static int read_int(const char *path, int def)
{
    char buf[32];
    int fd = open(path, O_RDONLY);
    if (fd < 0) return def;
    int n = read(fd, buf, sizeof buf - 1);
    close(fd);
    if (n <= 0) return def;
    buf[n] = 0;
    return atoi(buf);
}

static void open_inputs(void)
{
    for (int i = 0; i < 16 && n_in < MAX_INPUT; i++) {
        char path[32], name[64] = "";
        snprintf(path, sizeof path, "/dev/input/event%d", i);
        int fd = open(path, O_RDONLY | O_NONBLOCK | O_CLOEXEC);
        if (fd < 0) continue;
        ioctl(fd, EVIOCGNAME(sizeof name), name);
        if (strstr(name, "accel")) { close(fd); continue; } /* not needed, saves wakeups */
        int is_touch = strstr(name, "cyttsp") || strstr(name, "touch") || strstr(name, "Touch");
        if (is_touch) {
            struct input_absinfo ai;
            if (!ioctl(fd, EVIOCGABS(ABS_MT_POSITION_X), &ai) && ai.maximum > 0) touch_max_x = ai.maximum;
            if (!ioctl(fd, EVIOCGABS(ABS_MT_POSITION_Y), &ai) && ai.maximum > 0) touch_max_y = ai.maximum;
            /* the kernel won't repeat the current slot number, so ask for it */
            if (!ioctl(fd, EVIOCGABS(ABS_MT_SLOT), &ai) && ai.value >= 0 && ai.value < 16) touch_initial_slot = ai.value;
        }
        in_fds[n_in] = fd;
        in_touch[n_in] = is_touch;
        n_in++;
    }
}

int plat_init(canvas_t *back)
{
    const char *t = getenv("COOK_TOUCH"); /* e.g. "flipx,swap" */
    if (t) {
        touch_flip_x = !!strstr(t, "flipx");
        touch_flip_y = !!strstr(t, "flipy");
        touch_swap = !!strstr(t, "swap");
    }
    if (getenv("COOK_MIRROR")) gfx_mirror = 1;

    back->w = SCREEN_W;
    back->h = SCREEN_H;
    back->pitch = SCREEN_H;

    if (getenv("COOK_SIM")) {
        plat_sim = 1;
        back->px = calloc((size_t)SCREEN_W * SCREEN_H, sizeof(px_t));
        return back->px ? 0 : -1;
    }

    fb_fd = open("/dev/fb0", O_RDWR | O_CLOEXEC);
    if (fb_fd < 0) fb_fd = open("/dev/graphics/fb0", O_RDWR | O_CLOEXEC);
    if (fb_fd < 0) return -1;
    ioctl(fb_fd, FBIOGET_VSCREENINFO, &vinfo);

    /* try two pages so each frame is shown whole (no tearing) */
    struct fb_var_screeninfo v2 = vinfo;
    v2.yres_virtual = v2.yres * 2;
    v2.yoffset = 0;
    if (!ioctl(fb_fd, FBIOPUT_VSCREENINFO, &v2)) ioctl(fb_fd, FBIOGET_VSCREENINFO, &vinfo);
    ioctl(fb_fd, FBIOGET_FSCREENINFO, &finfo);
    if (vinfo.yres_virtual >= vinfo.yres * 2 &&
        finfo.smem_len >= finfo.line_length * vinfo.yres * 2)
        fb_pages = 2;

    fb_mem = mmap(NULL, finfo.smem_len, PROT_READ | PROT_WRITE, MAP_SHARED, fb_fd, 0);
    if (fb_mem == MAP_FAILED) return -1;

    /* the back buffer is laid out exactly like one framebuffer page */
    back->pitch = finfo.line_length / 2;
    back->h = vinfo.xres < SCREEN_H ? vinfo.xres : SCREEN_H;
    back->w = vinfo.yres < SCREEN_W ? vinfo.yres : SCREEN_W;
    back->px = calloc((size_t)back->pitch * back->w, sizeof(px_t));
    if (!back->px) return -1;

    open_inputs();
    if (!access("/sys/power/wait_for_fb_sleep", R_OK) && !pipe(fbwake_pipe)) {
        fcntl(fbwake_pipe[0], F_SETFL, O_NONBLOCK);
        pthread_t th;
        pthread_create(&th, NULL, fb_handshake, NULL);
        pthread_detach(th);
    }
    return 0;
}

void plat_present(canvas_t *back)
{
    if (plat_sim) return;
    size_t page_bytes = (size_t)finfo.line_length * vinfo.yres;
    if (fb_pages == 2) fb_page ^= 1;
    memcpy(fb_mem + fb_page * page_bytes, back->px, page_bytes);
    if (fb_pages == 2) {
        vinfo.yoffset = fb_page * vinfo.yres;
        ioctl(fb_fd, FBIOPAN_DISPLAY, &vinfo);
        int zero = 0;
        ioctl(fb_fd, FBIO_WAITFORVSYNC, &zero);
    }
}

int plat_input_fds(int *fds, int max)
{
    int n = n_in < max ? n_in : max;
    memcpy(fds, in_fds, n * sizeof(int));
    if (fbwake_pipe[0] >= 0 && n < max) fds[n++] = fbwake_pipe[0];
    return n;
}

/* ---- touch state machine (multitouch protocol A or B; we follow one finger) ---- */

/* Every finger gets a numbered slot. The kernel only announces a slot
 * number when it changes, so after a two-finger touch single taps may keep
 * arriving in slot 1, 2, and so on. We follow the first finger down,
 * whatever its slot, until every finger is lifted. */
#define SLOTS 16

static struct {
    int slot;          /* slot the following events describe */
    int id[SLOTS];     /* tracking id, -1 when no finger */
    int sx[SLOTS], sy[SLOTS];
    int primary;       /* slot we follow, -1 when none */
    int down;          /* last reported state */
    int x, y, dirty;
    int a_contacts, a_seen; /* protocol A bookkeeping */
} ts = { .primary = -1 };

#define QLEN 64
static event_t q[QLEN];
static int qh, qt;

static void push(event_t e)
{
    if ((qt + 1) % QLEN == qh) return;
    q[qt] = e;
    qt = (qt + 1) % QLEN;
}

static void map_touch(int rx, int ry, int *x, int *y)
{
    if (touch_swap) { int t = rx; rx = ry; ry = t; }
    int mx = touch_swap ? touch_max_y : touch_max_x;
    int my = touch_swap ? touch_max_x : touch_max_y;
    *x = rx * SCREEN_W / (mx ? mx : 1);
    *y = ry * SCREEN_H / (my ? my : 1);
    if (touch_flip_x) *x = SCREEN_W - 1 - *x;
    if (touch_flip_y) *y = SCREEN_H - 1 - *y;
}

static void touch_sync(void)
{
    int down;
    if (ts.a_seen) {
        down = ts.a_contacts > 0;
    } else {
        int any = 0;
        for (int i = 0; i < SLOTS; i++) any |= ts.id[i] >= 0;
        if (ts.primary == -1 && any && !ts.down) {
            for (int i = 0; i < SLOTS; i++)
                if (ts.id[i] >= 0) { ts.primary = i; break; }
        }
        if (ts.primary >= 0) {
            ts.x = ts.sx[ts.primary];
            ts.y = ts.sy[ts.primary];
        }
        down = ts.primary >= 0 && ts.id[ts.primary] >= 0;
        if (!down) ts.primary = -1;
        /* don't start a new gesture until all fingers are off */
        if (!down && any) ts.primary = -2;
        if (!any && ts.primary == -2) ts.primary = -1;
    }
    event_t e = { 0 };
    map_touch(ts.x, ts.y, &e.x, &e.y);
    if (down && !ts.down) e.type = EV_TOUCH_DOWN;
    else if (down && ts.dirty) e.type = EV_TOUCH_MOVE;
    else if (!down && ts.down) e.type = EV_TOUCH_UP;
    if (e.type) push(e);
    ts.down = down;
    ts.dirty = 0;
    ts.a_contacts = 0;
}

/* The kernel (32-bit, 2012) sends 16-byte events with 32-bit timestamps.
 * musl's struct input_event uses 64-bit time, so read our own layout. */
typedef struct {
    uint32_t sec, usec;
    uint16_t type, code;
    int32_t value;
} raw_event_t;

static void handle(int touch, raw_event_t *ie)
{
    if (ie->type == EV_KEY && !touch) {
        if (ie->value == 2) return; /* autorepeat */
        event_t e = { .type = ie->value ? EV_KEY_DOWN : EV_KEY_UP, .key = ie->code };
        push(e);
        return;
    }
    if (!touch) return;
    static int started;
    if (!started) {
        started = 1;
        ts.slot = touch_initial_slot;
        for (int i = 0; i < SLOTS; i++) ts.id[i] = -1;
    }
    if (ie->type == EV_ABS) {
        switch (ie->code) {
        case ABS_MT_SLOT: ts.slot = ie->value >= 0 && ie->value < SLOTS ? ie->value : 0; break;
        case ABS_MT_TRACKING_ID: ts.id[ts.slot] = ie->value; break;
        case ABS_MT_POSITION_X:
            if (ts.a_seen) { if (!ts.a_contacts) ts.x = ie->value; }
            else ts.sx[ts.slot] = ie->value;
            if (ts.slot == ts.primary || ts.a_seen) ts.dirty = 1;
            break;
        case ABS_MT_POSITION_Y:
            if (ts.a_seen) { if (!ts.a_contacts) ts.y = ie->value; }
            else ts.sy[ts.slot] = ie->value;
            if (ts.slot == ts.primary || ts.a_seen) ts.dirty = 1;
            break;
        }
    } else if (ie->type == EV_SYN) {
        if (ie->code == SYN_MT_REPORT) { ts.a_seen = 1; ts.a_contacts++; }
        else if (ie->code == SYN_REPORT) touch_sync();
    }
}

int plat_next_event(event_t *ev)
{
    if (qh == qt && !plat_sim) {
        char w[8];
        if (fbwake_pipe[0] >= 0 && read(fbwake_pipe[0], w, sizeof w) > 0) push((event_t){ .type = EV_REDRAW });
        raw_event_t buf[32];
        for (int i = 0; i < n_in; i++) {
            ssize_t n;
            while ((n = read(in_fds[i], buf, sizeof buf)) > 0)
                for (size_t k = 0; k < n / sizeof buf[0]; k++) handle(in_touch[i], &buf[k]);
        }
    }
    if (qh == qt) return 0;
    *ev = q[qh];
    qh = (qh + 1) % QLEN;
    return 1;
}

/* ---- power ---- */

static int backlight_now = -1;

void plat_backlight(int percent)
{
    if (percent < 0) percent = 0;
    if (percent > 100) percent = 100;
    if (percent == backlight_now) return;
    backlight_now = percent;
    char buf[16];
    /* the panel is unreadable below ~8/255, so map 1..100 onto 8..255 */
    snprintf(buf, sizeof buf, "%d", percent ? 8 + percent * 247 / 100 : 0);
    write_file("/sys/class/leds/lcd-backlight/brightness", buf);
}

int plat_battery(int *charging)
{
    if (plat_sim) { *charging = 0; return 87; }
    char st[32] = "";
    int fd = open("/sys/class/power_supply/max17042-0/status", O_RDONLY);
    if (fd >= 0) { read(fd, st, sizeof st - 1); close(fd); }
    /* "Charging" only means a charger is attached; a weak one can still lose
     * charge, so trust the measured current instead */
    int plugged = !strncmp(st, "Charging", 8) || !strncmp(st, "Full", 4);
    int current = read_int("/sys/class/power_supply/max17042-0/current_now", 0);
    *charging = !plugged ? 0 : (current > 0 || !strncmp(st, "Full", 4)) ? 1 : 2;
    return read_int("/sys/class/power_supply/max17042-0/capacity", -1);
}

/* developer mode keeps Wi-Fi up while awake; it would also keep the chip
 * from sleeping, so turn it off for sleep and back on at wake */
static int devmode(void)
{
    return !access("/data/cook/devmode", F_OK);
}

void plat_sleep(void)
{
    plat_backlight(0);
    if (devmode()) system("WIFI_FORCE=1 wifi down >/dev/null 2>&1");
    write_file("/sys/power/state", "mem");
}

void plat_wake(void)
{
    write_file("/sys/power/state", "on");
    backlight_now = -1;
    if (devmode()) system("(wifi online >/tmp/wifi.log 2>&1 &)");
}

void plat_wakelock(int on)
{
    write_file(on ? "/sys/power/wake_lock" : "/sys/power/wake_unlock", "cook");
}

int plat_key_held(int key)
{
    open_inputs();
    for (int i = 0; i < n_in; i++) {
        unsigned char bits[KEY_MAX / 8 + 1];
        memset(bits, 0, sizeof bits);
        if (ioctl(in_fds[i], EVIOCGKEY(sizeof bits), bits) >= 0 && (bits[key / 8] & (1 << (key % 8))))
            return 1;
    }
    return 0;
}

/* ---- simulator ---- */

static FILE *sim_script;

static unsigned crc_table[256];

static unsigned crc(unsigned c, const unsigned char *p, size_t n)
{
    if (!crc_table[1])
        for (unsigned i = 0; i < 256; i++) {
            unsigned v = i;
            for (int k = 0; k < 8; k++) v = v & 1 ? 0xedb88320u ^ (v >> 1) : v >> 1;
            crc_table[i] = v;
        }
    c = ~c;
    while (n--) c = crc_table[(c ^ *p++) & 255] ^ (c >> 8);
    return ~c;
}

static void put32(unsigned char *p, unsigned v) { p[0] = v >> 24; p[1] = v >> 16; p[2] = v >> 8; p[3] = v; }

static void chunk(FILE *f, const char *type, const unsigned char *d, unsigned n)
{
    unsigned char h[8];
    put32(h, n);
    memcpy(h + 4, type, 4);
    fwrite(h, 1, 8, f);
    if (n) fwrite(d, 1, n, f);
    unsigned c = crc(crc(0, (const unsigned char *)type, 4), d, n);
    put32(h, c);
    fwrite(h, 1, 4, f);
}

/* uncompressed PNG of the portrait screen */
static void write_png(const char *path, canvas_t *c)
{
    FILE *f = fopen(path, "wb");
    if (!f) return;
    int w = c->w, h = c->h, rowlen = 1 + w * 3;
    size_t raw_n = (size_t)rowlen * h;
    unsigned char *raw = malloc(raw_n);
    for (int y = 0; y < h; y++) {
        unsigned char *r = raw + (size_t)y * rowlen;
        *r++ = 0;
        for (int x = 0; x < w; x++) {
            px_t p = c->px[(size_t)PANEL_ROW(c, x) * c->pitch + y];
            *r++ = (p >> 11) << 3;
            *r++ = ((p >> 5) & 63) << 2;
            *r++ = (p & 31) << 3;
        }
    }
    size_t blocks = (raw_n + 65534) / 65535;
    size_t z_n = 2 + raw_n + blocks * 5 + 4;
    unsigned char *z = malloc(z_n), *zp = z;
    *zp++ = 0x78; *zp++ = 0x01;
    unsigned a = 1, b = 0;
    for (size_t off = 0; off < raw_n; off += 65535) {
        size_t n = raw_n - off < 65535 ? raw_n - off : 65535;
        *zp++ = off + n == raw_n;
        *zp++ = n & 255; *zp++ = n >> 8; *zp++ = ~n & 255; *zp++ = (~n >> 8) & 255;
        memcpy(zp, raw + off, n);
        zp += n;
        for (size_t i = 0; i < n; i++) { a = (a + raw[off + i]) % 65521; b = (b + a) % 65521; }
    }
    put32(zp, (b << 16) | a);
    fwrite("\x89PNG\r\n\x1a\n", 1, 8, f);
    unsigned char ihdr[13];
    put32(ihdr, w); put32(ihdr + 4, h);
    ihdr[8] = 8; ihdr[9] = 2; ihdr[10] = ihdr[11] = ihdr[12] = 0;
    chunk(f, "IHDR", ihdr, 13);
    chunk(f, "IDAT", z, z_n);
    chunk(f, "IEND", NULL, 0);
    fclose(f);
    free(raw);
    free(z);
}

int plat_sim_step(event_t *ev, canvas_t *back)
{
    if (qh != qt) return plat_next_event(ev);
    if (!sim_script) sim_script = fopen(getenv("COOK_SIM"), "r");
    if (!sim_script) return 0;
    char line[256];
    while (fgets(line, sizeof line, sim_script)) {
        int x, y, x2, y2;
        char arg[200];
        if (sscanf(line, "tap %d %d", &x, &y) == 2) {
            push((event_t){ .type = EV_TOUCH_DOWN, .x = x, .y = y });
            push((event_t){ .type = EV_TOUCH_UP, .x = x, .y = y });
        } else if (sscanf(line, "drag %d %d %d %d", &x, &y, &x2, &y2) == 4) {
            push((event_t){ .type = EV_TOUCH_DOWN, .x = x, .y = y });
            for (int i = 1; i <= 10; i++)
                push((event_t){ .type = EV_TOUCH_MOVE, .x = x + (x2 - x) * i / 10, .y = y + (y2 - y) * i / 10 });
            push((event_t){ .type = EV_TOUCH_UP, .x = x2, .y = y2 });
        } else if (sscanf(line, "key %199s", arg) == 1) {
            int k = !strcmp(arg, "home") ? K_HOME : !strcmp(arg, "power") ? K_POWER :
                    !strcmp(arg, "volup") ? K_VOLUP : K_VOLDOWN;
            push((event_t){ .type = EV_KEY_DOWN, .key = k });
            push((event_t){ .type = EV_KEY_UP, .key = k });
        } else if (sscanf(line, "shot %199s", arg) == 1) {
            write_png(arg, back);
            continue;
        } else {
            continue;
        }
        return plat_next_event(ev);
    }
    return 0;
}
