/* Hardware access: screen, touch, buttons, backlight, battery, and sleep. */
#ifndef PLATFORM_H
#define PLATFORM_H

#include "gfx.h"

#define SCREEN_W 600
#define SCREEN_H 1024

enum ev_type { EV_NONE, EV_TOUCH_DOWN, EV_TOUCH_MOVE, EV_TOUCH_UP, EV_KEY_DOWN, EV_KEY_UP, EV_REDRAW };

/* key codes we care about (match linux/input.h) */
#define K_HOME 102
#define K_VOLDOWN 114
#define K_VOLUP 115
#define K_POWER 116

typedef struct {
    int type;
    int x, y; /* touch, portrait coordinates */
    int key;
} event_t;

int  plat_init(canvas_t *back);
void plat_present(canvas_t *back);
/* fds to poll for input; returns count */
int  plat_input_fds(int *fds, int max);
/* reads pending input; returns 1 and fills *ev while events remain */
int  plat_next_event(event_t *ev);

void plat_backlight(int percent); /* 0..100 */
int  plat_battery(int *charging);  /* percent or -1; charging: 0 no, 1 yes, 2 plugged in but draining */
void plat_sleep(void);             /* screen off, let the CPU suspend */
void plat_wake(void);
void plat_wakelock(int on);
int  plat_key_held(int key);       /* for the boot-time escape hatch */

long long now_ms(void);

/* simulator (COOK_SIM=script): no hardware, scripted input, PNG screenshots */
extern int plat_sim;
int  plat_sim_step(event_t *ev, canvas_t *back); /* 0 when the script ends */

#endif
