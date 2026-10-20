# Handoff: Nook Recipes

Status as of 2026-10-20, end of the first session. For how the system works, see [README.md](README.md).

## Where things stand

The Nook runs the new system from the boot loader's alternate slot. Android is still installed as a fallback.

**The battery is dead.** It died during testing: it started at 11% on a weak charger. The user is charging it. Nothing is broken.

The last full install (with every fix below) was written to the boot partition, and its checksum was verified. The Nook rebooted, but the battery died before I could confirm the boot. **First step next session:** once it's charged and on, confirm it boots to the recipe list and that SSH works.

## Confirmed working on the device

- Boots straight to the recipe list. The system uses about 10 MB of RAM (Android used 417 MB).
- The screen is upright and readable, with double buffering active.
- Touch: tapping, dragging, scrolling, and checking items off. Also recovers after two-finger and palm touches.
- The "n" button goes back. Volume buttons change brightness, with an on-screen message.
- The power button turns the screen off and on, without the 1-second delay.
- Wi-Fi connects at boot in developer mode, using the networks carried over from Android. SSH works.

## Built but not yet tested on the device

- **Sync end to end:** HTTPS download with curl, setting the clock over NTP, and replacing the recipes.
- **Wi-Fi setup screen:** scanning and entering a password on the device. The scan list and keyboard were only tested in the simulator.
- **Deep sleep battery drain with developer mode off.** With it on, Wi-Fi stays up.
- **Escape hatch:** holding "n" while powering on should delete `/rom/u-boot.altboot` and reboot into Android.
- **Power off:** from Settings, and by holding the power button.
- **Charging indicator:** the battery icon now shows a bolt only when current is flowing in, and Settings says "plugged in but not charging" for a weak charger.

## Device access

- **SSH (developer mode):** `ssh -i .dev\id_ed25519 root@192.168.1.50`. The IP comes from the router's DHCP, so scan port 22 if it changes. The device has no `scp`; `tools/deploy.ps1` uses `cmd /c "ssh ... < file"`, because PowerShell 5.1 pipes corrupt binary data.
- **ADB:** only when it's booted into Android, and only over Wi-Fi (`adb connect 192.168.1.50:5555`). The USB data connection never worked with the cables available.
- **Screenshots:** `dd if=/dev/fb0` on the device, base64 it over SSH, then `tools/fb2png.py`.
- An onn 4K streaming box is also paired with adb on this PC. Don't touch it.

## Hardware notes

- **Charger:** the original Barnes & Noble charger isn't available. Other chargers get 500 mA, which is less than the Nook uses with the screen and Wi-Fi on. It charges only when it's off or asleep.
- **Power draw:** the battery reads `/sys/class/power_supply/max17042-0/current_now`, which is negative while discharging.
- **Bluetooth:** "stk ldisc" errors in dmesg come from Bluetooth probing. They're harmless, though the driver could be left unloaded.

## Next steps

1. **Confirm the boot** after charging (see above).
2. **Recipes:** the user said they'll tailor recipes to whatever format is needed. Give them the format from the README. Help them host the files (a GitHub repo with raw links works) plus a list file. Set the address in Settings → Recipe source.
3. **Test a real sync** and the Wi-Fi setup screen on the device.
4. **Turn off developer mode:** delete `/data/cook/devmode` and reboot. Then measure sleep drain from the change in `current_now` and battery percentage overnight.
5. **Remove Android** when the user is ready. That was the plan from the start; they don't need it.
   - Copy `uAltImg` and `uAltRam` over `uImage` and `uRamdisk` on partition 1, then delete `/rom/u-boot.altboot`. The normal boot path then runs this system.
   - Partitions 5 to 7 (system, data, and cache) can then be wiped, or left alone. Keep partitions 1 (boot), 2 (`/rom`: MAC address, calibration, boot counter), 3 (factory), and 8 (`/data`).
   - After that, the "n" escape hatch has nothing to go back to, so remove the "Restart into Android" row in Settings. It hides itself automatically once `u-boot.altboot` is gone.
   - **Recovery plan before you do this:** if the system ever fails to boot without Android, the fallback is booting from an SD card. That needs the card's partition marked active, which requires admin rights on Windows. The user offered admin rights if needed.
6. **Optional polish:**
   - The version label at the bottom of Settings sits close to the last row.
   - The checked-off state of recipe items resets when the app restarts.
   - Settings could expose turning developer mode off.

## Decisions made with the user

- They don't care about keeping Android. Speed and battery life matter most.
- Install to internal storage. Their 16 GB SD card holds old Nook data (photos, eBooks, and backups) and was left untouched.
- Keep the stock 3.0.8 kernel rather than a newer one. The user asked; newer mainline kernels only partly support this device.
- Volume buttons control brightness, not scrolling.
- Screen timeout: 3 minutes on the list and settings; on a recipe, configurable (default 30 minutes, or "Always").
