# Registers "VixxWatch" scheduled task: runs vixx_watch.py daily, and catches
# up as soon as the PC is on if a run was missed. Run in PowerShell:
#   powershell -ExecutionPolicy Bypass -File .\install_windows.ps1

$ErrorActionPreference = "Stop"

$Dir    = Split-Path -Parent $MyInvocation.MyCommand.Definition
$Script = Join-Path $Dir "vixx_watch.py"

# Resolve a real (non-Store-shim) python.exe
$Py = python -c "import sys; print(sys.executable)" 2>$null
if (-not $Py -or $Py -like "*WindowsApps*") {
    throw "Could not resolve a real python.exe (got '$Py'). Install python.org build."
}

Write-Host "vixx-watch dir : $Dir"
Write-Host "python.exe     : $Py"
Write-Host "script         : $Script"

# --- Task 1: daily crawl + diff (queues pages for archiving) ---
$crawlAction = New-ScheduledTaskAction -Execute $Py -Argument "`"$Script`"" -WorkingDirectory $Dir
$crawlTrigger = New-ScheduledTaskTrigger -Daily -At 9:00am
$crawlSettings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable -DontStopOnIdleEnd `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 15) `
    -RestartCount 2 -RestartInterval (New-TimeSpan -Minutes 10)
Register-ScheduledTask -TaskName "VixxWatch" -Action $crawlAction -Trigger $crawlTrigger `
    -Settings $crawlSettings -Description "Daily vixx.vn change monitor (crawl + diff)" -Force | Out-Null

# --- Task 2: spaced-out archiver — one page to Wayback every 2 hours ---
$arcAction = New-ScheduledTaskAction -Execute $Py -Argument "`"$Script`" --archive" -WorkingDirectory $Dir
$arcTrigger = New-ScheduledTaskTrigger -Once -At 9:30am -RepetitionInterval (New-TimeSpan -Hours 2)
$arcSettings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable -DontStopOnIdleEnd `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 10)
Register-ScheduledTask -TaskName "VixxWatchArchive" -Action $arcAction -Trigger $arcTrigger `
    -Settings $arcSettings -Description "vixx.vn Wayback archiver (1 page / 2h, spaced)" -Force | Out-Null

Write-Host "`nRegistered tasks:"
Write-Host "  VixxWatch        - daily 09:00 local (crawl + diff; catches up if PC was off)"
Write-Host "  VixxWatchArchive - every 2h from 09:30 (archives 1 page/run, spread over the day)"
Write-Host "Manage:  Get-ScheduledTask VixxWatch*, Get-ScheduledTaskInfo VixxWatch"
Write-Host "Run now: Start-ScheduledTask VixxWatch ; Start-ScheduledTask VixxWatchArchive"
Write-Host "Remove:  Unregister-ScheduledTask VixxWatch,VixxWatchArchive -Confirm:`$false"
