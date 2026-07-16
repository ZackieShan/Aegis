# Aegis one-line installer for Windows.
#
#   irm https://raw.githubusercontent.com/ZackieShan/Aegis/main/install.ps1 | iex
#
# Gets you from nothing to a running Aegis with a Start Menu icon:
# checks for git + Python (installs via winget if missing), clones the repo
# to ~\Aegis (or updates it), creates the "Aegis" Start Menu shortcut, and
# launches. Safe to re-run — it updates instead of re-installing.
#
# (No param() block: this file is piped through Invoke-Expression, which
# doesn't accept params. Override the install location with $env:AEGIS_HOME.)

$ErrorActionPreference = "Stop"
function Step($m) { Write-Host ""; Write-Host ("==> " + $m) -ForegroundColor Cyan }

$dest = if ($env:AEGIS_HOME) { $env:AEGIS_HOME } else { Join-Path $env:USERPROFILE "Aegis" }

Step "Checking prerequisites"
$needGit = -not (Get-Command git -ErrorAction SilentlyContinue)
$needPy  = -not (Get-Command python -ErrorAction SilentlyContinue)
if ($needGit -or $needPy) {
    if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
        Write-Host "Please install Git (https://git-scm.com) and Python 3.11+ (https://python.org), then re-run." -ForegroundColor Yellow
        return
    }
    if ($needGit) { Step "Installing Git via winget"; winget install --id Git.Git --accept-package-agreements --accept-source-agreements --silent | Out-Null }
    if ($needPy)  { Step "Installing Python via winget"; winget install --id Python.Python.3.12 --accept-package-agreements --accept-source-agreements --silent | Out-Null }
    # Refresh PATH for this session so the fresh installs are visible.
    $env:Path = [Environment]::GetEnvironmentVariable("Path", "Machine") + ";" + [Environment]::GetEnvironmentVariable("Path", "User")
}

if (Test-Path (Join-Path $dest ".git")) {
    Step "Updating existing install at $dest"
    git -C $dest pull --ff-only
} else {
    Step "Downloading Aegis to $dest"
    git clone --depth 1 https://github.com/ZackieShan/Aegis.git $dest
}

Step "Creating the Start Menu shortcut"
powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $dest "scripts\create-shortcuts.ps1")

Step "Starting Aegis (first run installs its Python packages - a few minutes)"
Write-Host "Next time: just open 'Aegis' from the Start Menu." -ForegroundColor Green
powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $dest "launch-windows.ps1") -OpenBrowser
