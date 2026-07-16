<#
  Creates "Aegis" Start Menu (and optionally Desktop) shortcuts, so starting
  the app is a double-click instead of a terminal command.

  The shortcut runs the normal launcher minimized with -OpenBrowser: the
  console sits in the taskbar (close it to stop Aegis), and the browser opens
  by itself once the server is up. Safe to re-run — it overwrites in place.

    powershell -ExecutionPolicy Bypass -File scripts\create-shortcuts.ps1
    powershell -ExecutionPolicy Bypass -File scripts\create-shortcuts.ps1 -Desktop
#>
param(
    [switch]$Desktop
)

$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $PSScriptRoot
$launcher = Join-Path $repo "launch-windows.ps1"
$icon = Join-Path $repo "static\icon.ico"
if (-not (Test-Path $launcher)) { throw "launch-windows.ps1 not found at $launcher" }

function New-AegisShortcut([string]$dir) {
    $ws = New-Object -ComObject WScript.Shell
    $lnk = $ws.CreateShortcut((Join-Path $dir "Aegis.lnk"))
    $lnk.TargetPath = "powershell.exe"
    $lnk.Arguments = "-NoProfile -ExecutionPolicy Bypass -File `"$launcher`" -OpenBrowser"
    $lnk.WorkingDirectory = $repo
    if (Test-Path $icon) { $lnk.IconLocation = $icon }
    $lnk.WindowStyle = 7  # minimized — the server console lives in the taskbar
    $lnk.Description = "Start Aegis and open it in your browser"
    $lnk.Save()
    Write-Host ("  [ok] " + (Join-Path $dir "Aegis.lnk"))
}

$startMenu = Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs"
New-AegisShortcut $startMenu
if ($Desktop) { New-AegisShortcut ([Environment]::GetFolderPath("Desktop")) }
Write-Host "Done. Search 'Aegis' in the Start Menu to launch."
