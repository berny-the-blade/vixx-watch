# Runs the monitor (crawl or --archive), regenerates docs/index.html, and
# pushes it to GitHub Pages. Called by the scheduled tasks.
#   .\run_and_publish.ps1            # daily crawl + diff
#   .\run_and_publish.ps1 -Archive   # spaced Wayback archive step
param([switch]$Archive, [switch]$News)
$ErrorActionPreference = "Continue"

$Dir = Split-Path -Parent $MyInvocation.MyCommand.Definition
$Py = python -c "import sys; print(sys.executable)" 2>$null
if (-not $Py -or $Py -like "*WindowsApps*") {
    $Py = "C:\Users\BERND\AppData\Local\Python\pythoncore-3.14-64\python.exe"
}
Set-Location $Dir

if     ($Archive) { & $Py "$Dir\vixx_watch.py" --archive }
elseif ($News)    { & $Py "$Dir\vixx_watch.py" --news }
else              { & $Py "$Dir\vixx_watch.py" }

# Publish the dashboard only if it actually changed.
git -C $Dir add docs 2>$null
$dirty = git -C $Dir status --porcelain docs
if ($dirty) {
    $stamp = (Get-Date).ToUniversalTime().ToString("yyyy-MM-dd HH:mm 'UTC'")
    git -C $Dir -c user.name="Bernd Sischka" -c user.email="bernd@power.trade" `
        commit -q -m "dashboard update $stamp" 2>&1 | Out-Null
    git -C $Dir push -q origin main 2>&1 | Out-Null
    Write-Host "published dashboard ($stamp)"
} else {
    Write-Host "no dashboard change to publish"
}
