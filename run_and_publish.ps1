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

$stamp = (Get-Date).ToUniversalTime().ToString("yyyy-MM-dd HH:mm 'UTC'")

# Publish the dashboard to NETLIFY (off GitHub Pages — no Actions/billing).
# Only on the daily crawl + news runs; skip the 2-hourly archive runs.
$SiteId = "f2f99f0d-0cb6-4efa-b098-f9e52944dc40"   # vixx-watch dashboard on Netlify
$Netlify = (Get-Command netlify -ErrorAction SilentlyContinue).Source
if (-not $Netlify) { $Netlify = "C:\Users\BERND\AppData\Roaming\npm\netlify.cmd" }
if ($Archive) {
    Write-Host "archive run: skipping dashboard deploy"
} else {
    & $Netlify deploy --prod --dir="$Dir\docs" --site $SiteId 2>&1 |
        Select-String -Pattern "Deploy is live|Website URL|Error" | ForEach-Object { $_.Line.Trim() }
    Write-Host "deployed dashboard to Netlify ($stamp)"
}

# Push the forensic evidence repo (separate PRIVATE repo) — off-machine,
# GitHub-timestamped witness of the captures + manifest + OTS proofs.
$Ev = Join-Path $Dir "evidence"
if (Test-Path (Join-Path $Ev ".git")) {
    git -C $Ev add -A 2>$null
    if (git -C $Ev status --porcelain) {
        git -C $Ev -c user.name="Bernd Sischka" -c user.email="bernd@power.trade" `
            commit -q -m "evidence $stamp" 2>&1 | Out-Null
        git -C $Ev push -q origin main 2>&1 | Out-Null
        Write-Host "pushed evidence ($stamp)"
    } else {
        Write-Host "no new evidence to push"
    }
}
