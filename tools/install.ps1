# Installs out/uAltImg and out/uAltRam on a Nook that is still running Android
# (over adb), then reboots it into the recipe system.
param([string]$Device = "192.168.1.50:5555")
$ErrorActionPreference = "Stop"
$root = Split-Path $PSScriptRoot
$adb = "$env:LOCALAPPDATA\Microsoft\WinGet\Packages\Google.PlatformTools_Microsoft.Winget.Source_8wekyb3d8bbwe\platform-tools\adb.exe"

& $adb connect $Device | Out-Null
& $adb -s $Device push "$root\out\uAltImg" "$root\out\uAltRam" "$root\.dev\id_ed25519.pub" /data/local/tmp/
& $adb -s $Device shell "mv /data/local/tmp/id_ed25519.pub /data/local/tmp/authorized_keys"
& $adb -s $Device push "$root\tools\nook-install.sh" /data/local/tmp/nook-install.sh
& $adb -s $Device shell "sh /data/local/tmp/nook-install.sh"
