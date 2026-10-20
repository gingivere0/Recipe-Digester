# Builds and updates the recipe system on a Nook that is already running it
# (developer mode, over SSH).
#   -AppOnly : swap in just the app for this boot (fast, lost on reboot)
#   default  : install the full system to the boot partition and reboot
param([string]$Nook = "192.168.1.50", [switch]$AppOnly, [switch]$NoBuild)
$ErrorActionPreference = "Continue"
$root = Split-Path $PSScriptRoot
$key = "$root\.dev\id_ed25519"
$ssh = "ssh -i `"$key`" -o StrictHostKeyChecking=no -o UserKnownHostsFile=NUL -o LogLevel=ERROR root@$Nook"

if (-not $NoBuild) {
    docker run --rm -v "${root}:/src" nook-build sh /src/build/build.sh 2>&1 | Select-Object -Last 3
    if ($LASTEXITCODE) { throw "build failed" }
}

# cmd's redirection keeps binary data intact (PowerShell 5.1's doesn't)
function Send($local, $remote) {
    cmd /c "$ssh `"cat > $remote`" < `"$local`""
    if ($LASTEXITCODE) { throw "copy failed: $local" }
}

if ($AppOnly) {
    Send "$root\out\cook" "/tmp/cook.new"
    cmd /c "$ssh `"chmod 755 /tmp/cook.new && mv /tmp/cook.new /usr/bin/cook && killall cook; echo app restarted`""
} else {
    Send "$root\out\uAltRam" "/tmp/uAltRam"
    Send "$root\out\uAltImg" "/tmp/uAltImg"
    cmd /c "$ssh `"mkdir -p /mnt/boot && mount -t vfat /dev/mmcblk0p1 /mnt/boot && cp /tmp/uAltRam /tmp/uAltImg /mnt/boot/ && sync && md5sum /tmp/uAltRam /mnt/boot/uAltRam && umount /mnt/boot && echo installed && reboot`""
}
