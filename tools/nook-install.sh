#!/system/bin/sh
# Runs on the Nook (under Android, as root). Installs the recipe system into
# the boot loader's "alternate" slot and makes it the default.
set -e
T=/data/local/tmp
B=$T/bootpart
D=/storage/sdcard0/cook   # internal partition 8, the new system's /data

mkdir -p $B
grep -q " $B " /proc/mounts || mount -t vfat /dev/block/mmcblk0p1 $B
cp $T/uAltImg $B/uAltImg
cp $T/uAltRam $B/uAltRam
sync
busybox md5sum $T/uAltImg $B/uAltImg $T/uAltRam $B/uAltRam
umount $B

mkdir -p $D
cp $T/authorized_keys $D/authorized_keys
touch $D/devmode
# carry over the Wi-Fi network Android already knows
if [ -f /data/misc/wifi/wpa_supplicant.conf ] && [ ! -f $D/wpa_supplicant.conf ]; then
    {
        echo "ctrl_interface=/var/run/wpa_supplicant"
        echo "update_config=1"
        busybox awk '/^network=\{/,/^\}/' /data/misc/wifi/wpa_supplicant.conf
    } > $D/wpa_supplicant.conf
fi

printf 1 > /rom/u-boot.altboot
sync
echo INSTALLED
