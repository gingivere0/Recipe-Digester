#!/bin/sh
# Builds the Nook system. Runs inside the nook-build container with the repo
# mounted at /src. Produces out/uAltImg (kernel) and out/uAltRam (system).
set -eu

SRC=/src
OUT=$SRC/out
R=/tmp/rootfs

rm -rf "$R"
mkdir -p "$OUT" "$R"/bin "$R"/sbin "$R"/usr/bin "$R"/usr/sbin "$R"/lib/modules "$R"/lib/firmware \
         "$R"/etc/ssl/certs "$R"/usr/share/fonts/dejavu "$R"/root "$R"/proc "$R"/sys "$R"/dev \
         "$R"/tmp "$R"/run "$R"/rom "$R"/data

echo "== app"
CFLAGS="-O2 -march=armv7-a -mtune=cortex-a8 -mfpu=neon -mfloat-abi=hard -Wall -Wno-unused-result"
gcc $CFLAGS -static -o "$R/usr/bin/cook" "$SRC/app/cook.c" "$SRC/app/gfx.c" "$SRC/app/platform.c" -lm
strip "$R/usr/bin/cook"
cp "$R/usr/bin/cook" "$OUT/cook"

echo "== userland"
cp /bin/busybox.static "$R/bin/busybox"

# copy a dynamic binary plus the libraries it needs
copy_bin() {
    cp "$1" "$R$2"
    ldd "$1" 2>/dev/null | awk '/=>/ { print $3 } /ld-musl/ { print $1 }' | while read -r lib; do
        [ -f "$lib" ] || continue
        mkdir -p "$R$(dirname "$lib")"
        cp -L "$lib" "$R$lib"
    done
}
copy_bin /sbin/wpa_supplicant /usr/sbin/wpa_supplicant
copy_bin /sbin/wpa_cli /usr/sbin/wpa_cli
copy_bin /sbin/wpa_passphrase /usr/sbin/wpa_passphrase
copy_bin /usr/bin/curl /usr/bin/curl
copy_bin /usr/sbin/dropbear /usr/sbin/dropbear
cp /lib/ld-musl-armhf.so.1 "$R/lib/"
find "$R/usr" "$R/lib" -type f \( -name '*.so*' -o -perm -u+x \) -exec strip --strip-unneeded {} + 2>/dev/null || true

cp /etc/ssl/certs/ca-certificates.crt "$R/etc/ssl/certs/"
cp /usr/share/fonts/dejavu/DejaVuSans.ttf /usr/share/fonts/dejavu/DejaVuSans-Bold.ttf "$R/usr/share/fonts/dejavu/"

echo "== drivers and firmware (from the Nook's own system)"
cp "$SRC"/backup/modules/compat.ko "$SRC"/backup/modules/cfg80211.ko "$SRC"/backup/modules/mac80211.ko \
   "$SRC"/backup/modules/wl12xx.ko "$SRC"/backup/modules/wl12xx_sdio.ko "$R/lib/modules/"
cp -r "$SRC"/backup/firmware/. "$R/lib/firmware/"

echo "== system files"
cp -r "$SRC"/rootfs/. "$R/"
# files edited on Windows may have CRLF endings
for f in "$R/init" "$R"/etc/inittab "$R"/etc/rc.* "$R"/etc/udhcpc.script "$R"/sbin/hotplug "$R"/usr/bin/wifi "$R"/usr/bin/cook-sync; do
    sed -i 's/\r$//' "$f"
done
chmod 755 "$R/init" "$R"/etc/rc.* "$R"/etc/udhcpc.script "$R"/sbin/hotplug "$R"/usr/bin/wifi "$R"/usr/bin/cook-sync

echo "== pack"
( cd "$R" && find . | sort | cpio -o -H newc --quiet ) | gzip -9 > /tmp/initramfs.gz
mkimage -A arm -O linux -T ramdisk -C none -a 0 -e 0 -n "nook-recipes" -d /tmp/initramfs.gz "$OUT/uAltRam" >/dev/null
cp "$SRC/backup/boot/uImage" "$OUT/uAltImg"

du -sh "$R"
ls -l "$OUT"
