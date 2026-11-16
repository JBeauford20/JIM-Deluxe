# Registers the JIM weekly ingest as a Windows Scheduled Task: Mondays 06:00.
# Run once (normal user, no admin needed):  powershell -File scripts\register_weekly_ingest.ps1
# Remove with:  Unregister-ScheduledTask -TaskName "JIM Weekly Ingest" -Confirm:$false
#
# The machine must be ON (or asleep with wake timers allowed) at 6am Monday.
# -WakeToRun wakes a sleeping machine; a powered-off laptop cannot run it,
# and the task then fires as soon as possible afterward (StartWhenAvailable).
# NOTE: keep this file ASCII-only - PowerShell 5.1 reads BOM-less files as ANSI.

$repo   = Split-Path $PSScriptRoot -Parent
$python = "C:\Python313\python.exe"
$script = Join-Path $repo "backend\ingest\weekly_ingest.py"
$logDir = Join-Path $repo "backend\ingest\logs"
New-Item -ItemType Directory -Force $logDir | Out-Null

$action = New-ScheduledTaskAction -Execute $python `
    -Argument "`"$script`"" -WorkingDirectory $repo

$trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday -At 6:00AM

$settings = New-ScheduledTaskSettingsSet -WakeToRun -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Hours 2) -MultipleInstances IgnoreNew

Register-ScheduledTask -TaskName "JIM Weekly Ingest" -Action $action `
    -Trigger $trigger -Settings $settings -Description `
    "Fetch DTS sales, load delivery manifest, recompute velocity for JIM Deluxe. Logs: backend\ingest\logs" -Force

Write-Host "Registered 'JIM Weekly Ingest' - Mondays 06:00. Logs land in $logDir"
Write-Host "Test it now with: Start-ScheduledTask -TaskName 'JIM Weekly Ingest'"
