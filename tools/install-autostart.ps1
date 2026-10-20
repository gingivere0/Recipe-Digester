# Registers the "Nook recipe server" scheduled task: starts the recipe server
# hidden at sign-in for the current user (no admin needed). Re-run to update.
# Remove with: Unregister-ScheduledTask -TaskName "Nook recipe server"
$ErrorActionPreference = "Stop"
$pythonw = Join-Path (Split-Path (& py -3 -c "import sys; print(sys.executable)")) "pythonw.exe"
$script = Join-Path $PSScriptRoot "serve-recipes-bg.pyw"
$user = "$env:USERDOMAIN\$env:USERNAME"

$action = New-ScheduledTaskAction -Execute $pythonw -Argument "`"$script`"" -WorkingDirectory $PSScriptRoot
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $user
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
    -MultipleInstances IgnoreNew
$principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited

Register-ScheduledTask -TaskName "Nook recipe server" -Action $action -Trigger $trigger `
    -Settings $settings -Principal $principal -Force | Out-Null
"Registered 'Nook recipe server'. Start it now with: schtasks /Run /TN `"Nook recipe server`""
