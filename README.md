# Nook Recipes

A barebones operating system for a Barnes & Noble Nook Color (BNRV200) that does two things: connects to Wi-Fi and shows recipes. It replaces Android with a tiny Linux system that boots straight into one fullscreen app.

For the current status and next steps, see [HANDOFF.md](HANDOFF.md).

## How it works

- **Kernel:** the Nook's own CyanogenMod 10 kernel (3.0.8), reused as is. It already has working drivers for the screen, touch, Wi-Fi, charging, and sleep.
- **System:** a 7.5 MB initramfs that runs entirely from RAM: static BusyBox, wpa_supplicant, curl, dropbear (developer mode only), DejaVu fonts, plus the Wi-Fi modules and firmware copied from the Nook.
- **App (`cook`):** about 100 KB of C. It draws straight to the framebuffer with stb_truetype and reads touch and buttons from evdev. It only renders when something changes, and otherwise sleeps in `poll()`.
- **Storage:** recipes and settings live on internal partition 8 (vfat), mounted at `/data`, under `/data/cook`.
- **Boot:** the stock boot loader's "alternate boot" slot. `uAltImg` and `uAltRam` sit on partition 1, and `/rom/u-boot.altboot` contains `1`.

### Device quirks the code handles

| Quirk | Where it's handled |
|---|---|
| The panel is 1024x600 landscape, but we use it in portrait. Canvases store pixels pre-rotated, so the back buffer is a plain copy of the framebuffer. | `app/gfx.h` |
| The kernel sends 16-byte input events, but musl's `struct input_event` is 24 bytes. | `raw_event_t` in `app/platform.c` |
| Touch uses multitouch slots, and the kernel only announces slot changes. We follow the first finger down in any slot. | `touch_sync()` in `app/platform.c` |
| The boot loader forces a factory restore after 8 boots unless `/rom/devconf/BootCnt` is reset. | `rootfs/init` |
| Kernel 3.0 loads firmware through a userspace helper. | `rootfs/sbin/hotplug` |
| Android's early-suspend handshake has to be acknowledged, or screen-off stalls for 1 second. | `fb_handshake()` in `app/platform.c` |

## Layout

```
app/            cook.c (UI), gfx.c/h (drawing, fonts, photos), platform.c/h (hardware), third_party/stb_truetype.h and stb_image.h
rootfs/         files copied into the system: init, inittab, rc scripts, wifi, cook-sync, sample recipe
build/          Dockerfile (ARMv7 Alpine build image), build.sh (produces out/)
tools/          install.ps1 (first install from Android over adb), deploy.ps1 (updates over SSH), fb2png.py (screenshots)
backup/         the Nook's original boot partition, kernel, modules, firmware, and /rom files (needed by the build)
.dev/           private SSH key for developer mode (keep private)
out/            build output: uAltImg, uAltRam, cook
```

## Building and deploying

Docker Desktop must be running. The build image is created once with:

```bash
docker build -t nook-build build
```

Build only:

```bash
docker run --rm -v C:\path\to\nook:/src nook-build sh /src/build/build.sh
```

Update a Nook that is running this system in developer mode:

```bash
powershell -ExecutionPolicy Bypass -File tools\deploy.ps1 -AppOnly
```

```bash
powershell -ExecutionPolicy Bypass -File tools\deploy.ps1
```

`-AppOnly` hot-swaps the app in RAM (lost on reboot). Without it, the full system is written to the boot partition and the Nook reboots.

### Simulator

The app runs without hardware when `COOK_SIM` points at a script of `tap x y`, `drag x1 y1 x2 y2`, `key home|power|volup|voldown`, and `shot file.png` lines. Set `COOK_DATA` to a folder with a `recipes/` subfolder. Build it natively inside `nook-build` with `gcc -O2 -o /tmp/cook app/*.c -lm`.

## Recipe format

Plain text or Markdown files (`.md` or `.txt`):

```
# Chocolate Chip Cookies
Makes 24 · 30 minutes

## Ingredients
- 2 cups flour
- 1 tsp baking soda

## Steps
1. Heat the oven to 375°F.
2. Whisk the dry ingredients.

## Notes
Any paragraph text.
```

The first line under the title becomes the subtitle in the list. The list opens on categories, taken from the file name prefix (`mains--chicken-parmesan.md` is in Mains; files without one go under Other), unless there's only one. Search covers every category. Each recipe in the list shows `<recipe name>.thumb.jpg` (80x80, downloaded with it when the server has one), or the title's first letter on a tile. Items and steps can be tapped to check them off. The ↺ button in the recipe's top bar unchecks them all, after asking.

A photo of the dish can go on its own line, usually under the subtitle: `![Photo](chocolate-chip-cookies.jpg)`. The link is relative to the recipe; sync downloads the JPEG or PNG next to it, and the app shows it across the text column (544 px wide fits without scaling).

**Sync source:** set in Settings as a web address. It points either at one recipe file or at a list file with one recipe link per line. Links can be full addresses or relative to the list. Blank lines and lines starting with `#` are ignored. A sync replaces all recipes on the Nook, but only if at least one recipe downloads. Photos linked from a recipe are downloaded with it.
