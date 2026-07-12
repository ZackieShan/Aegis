<#
.SYNOPSIS
  Sets up Aegis's local inference engine - the differentiated binaries that live
  OUTSIDE the repo so they never enter git: llama.cpp + llama-swap (GGUF serving),
  a portable Node + Playwright (browser automation), Aider (coding agent),
  faster-whisper (voice), and stable-diffusion.cpp (image generation).

  Everything installs under <repo>\..\engine (override with -EngineDir or the
  AEGIS_ENGINE_DIR env var). Idempotent: already-present pieces are skipped.
  launch-windows.ps1 auto-starts llama-swap from this same location.

.EXAMPLE
  .\scripts\setup-engine.ps1
  .\scripts\setup-engine.ps1 -SkipImageGen        # skip the ~1.5GB sd.cpp download
  .\scripts\setup-engine.ps1 -SkipBrowser         # skip Node/Playwright

.NOTES
  Windows + an NVIDIA GPU (the prebuilt binaries are CUDA 12). Requires the app
  venv to already exist (run launch-windows.ps1 once first, or `python -m venv venv`).
#>
[CmdletBinding()]
param(
  [string]$EngineDir = "",
  [switch]$SkipBrowser,
  [switch]$SkipImageGen,
  [switch]$SkipAider
)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"   # faster Invoke-WebRequest
$RepoRoot = Split-Path -Parent $PSScriptRoot

if (-not $EngineDir) {
  $EngineDir = if ($env:AEGIS_ENGINE_DIR) { $env:AEGIS_ENGINE_DIR } else { Join-Path (Split-Path -Parent $RepoRoot) "engine" }
}
New-Item -ItemType Directory -Force -Path $EngineDir | Out-Null
Write-Host "Engine dir: $EngineDir" -ForegroundColor Cyan

function Step($msg) { Write-Host "`n==> $msg" -ForegroundColor Green }
function Skip($msg) { Write-Host "  - $msg (already present, skipping)" -ForegroundColor DarkGray }

# Download the newest GitHub release asset whose name matches $Pattern, extract to $Dest.
function Get-GhAsset {
  param([string]$Repo, [string]$Pattern, [string]$Dest)
  $rel = Invoke-RestMethod "https://api.github.com/repos/$Repo/releases/latest" -Headers @{ "User-Agent" = "aegis-setup" }
  $asset = $rel.assets | Where-Object { $_.name -match $Pattern } | Select-Object -First 1
  if (-not $asset) {
    Write-Warning "No asset matching /$Pattern/ in $Repo $($rel.tag_name). Grab it manually from https://github.com/$Repo/releases and extract to $Dest"
    return $false
  }
  $zip = Join-Path $env:TEMP $asset.name
  Write-Host "  downloading $($asset.name) ($([math]::Round($asset.size/1MB)) MB)..."
  Invoke-WebRequest $asset.browser_download_url -OutFile $zip
  New-Item -ItemType Directory -Force -Path $Dest | Out-Null
  Expand-Archive -Path $zip -DestinationPath $Dest -Force
  Remove-Item $zip -Force
  return $true
}

# -- 1. llama.cpp (CUDA) - serves GGUFs, emits native tool calls -----------------
Step "llama.cpp (CUDA server)"
$llamaDir = Join-Path $EngineDir "llamacpp"
if (Test-Path (Join-Path $llamaDir "llama-server.exe")) { Skip "llama-server.exe" }
else {
  Get-GhAsset -Repo "ggml-org/llama.cpp" -Pattern "bin-win-cuda.*x64\.zip$" -Dest $llamaDir | Out-Null
  # the CUDA runtime DLLs ship as a separate asset - extract alongside
  Get-GhAsset -Repo "ggml-org/llama.cpp" -Pattern "cudart.*win.*x64\.zip$" -Dest $llamaDir | Out-Null
}

# -- 2. llama-swap - one endpoint, hot-swaps GGUFs, honors -watch-config ----------
Step "llama-swap"
$swapDir = Join-Path $EngineDir "llama-swap"
if (Test-Path (Join-Path $swapDir "llama-swap.exe")) { Skip "llama-swap.exe" }
else { Get-GhAsset -Repo "mostlygeek/llama-swap" -Pattern "windows.*amd64\.zip$" -Dest $swapDir | Out-Null }

# -- 3. portable Node + Playwright MCP - browser automation ----------------------
if ($SkipBrowser) { Step "Node/Playwright - skipped (-SkipBrowser)" }
else {
  Step "portable Node.js"
  $nodeDir = Join-Path $EngineDir "node"
  if (Test-Path (Join-Path $nodeDir "node.exe")) { Skip "node.exe" }
  else {
    $idx = Invoke-RestMethod "https://nodejs.org/dist/index.json"
    $lts = ($idx | Where-Object { $_.lts } | Select-Object -First 1).version
    $url = "https://nodejs.org/dist/$lts/node-$lts-win-x64.zip"
    Write-Host "  downloading Node $lts..."
    $zip = Join-Path $env:TEMP "node.zip"
    Invoke-WebRequest $url -OutFile $zip
    Expand-Archive $zip -DestinationPath $EngineDir -Force
    Move-Item (Join-Path $EngineDir "node-$lts-win-x64") $nodeDir -Force
    Remove-Item $zip -Force
  }
  Step "Playwright MCP + Chromium (browser)"
  $npx = Join-Path $nodeDir "npx.cmd"
  & $npx -y "@playwright/mcp@latest" --version | Out-Null
  & $npx -y "@playwright/mcp@latest" install-browser chromium
}

# -- 4. Aider - the coding agent (isolated venv so its deps can't clash) ----------
if ($SkipAider) { Step "Aider - skipped (-SkipAider)" }
else {
  Step "Aider (isolated venv)"
  $aiderVenv = Join-Path $EngineDir "aider-venv"
  if (Test-Path (Join-Path $aiderVenv "Scripts\python.exe")) { Skip "aider-venv" }
  else {
    python -m venv $aiderVenv
    & (Join-Path $aiderVenv "Scripts\python.exe") -m pip install --quiet --upgrade pip
    & (Join-Path $aiderVenv "Scripts\python.exe") -m pip install --quiet aider-chat
  }
}

# -- 5. faster-whisper + gguf - voice STT + context auto-tuner (into app venv) ----
Step "faster-whisper + gguf (into the app venv)"
$appPy = Join-Path $RepoRoot "venv\Scripts\python.exe"
if (Test-Path $appPy) {
  & $appPy -m pip install --quiet faster-whisper gguf
} else {
  Write-Warning "App venv not found at $appPy - run launch-windows.ps1 once, then: venv\Scripts\pip install faster-whisper gguf"
}

# -- 6. stable-diffusion.cpp - local image generation ----------------------------
if ($SkipImageGen) { Step "stable-diffusion.cpp - skipped (-SkipImageGen)" }
else {
  Step "stable-diffusion.cpp (image generation)"
  $sdDir = Join-Path $EngineDir "sdcpp"
  if (Test-Path (Join-Path $sdDir "sd-server.exe")) { Skip "sd-server.exe" }
  else {
    Get-GhAsset -Repo "leejet/stable-diffusion.cpp" -Pattern "bin-win-cuda12-x64\.zip$" -Dest $sdDir | Out-Null
    Get-GhAsset -Repo "leejet/stable-diffusion.cpp" -Pattern "cudart-.*win-cu12-x64\.zip$" -Dest $sdDir | Out-Null
  }
}

# -- 7. starter llama-swap.yaml --------------------------------------------------
Step "llama-swap config"
$cfg = Join-Path $EngineDir "llama-swap.yaml"
if (Test-Path $cfg) { Skip "llama-swap.yaml" }
else {
  Copy-Item (Join-Path $RepoRoot "docs\llama-swap.example.yaml") $cfg
  Write-Host "  wrote a starter $cfg - edit it to point at your GGUFs in models\." -ForegroundColor Yellow
}

Write-Host "`nEngine setup complete." -ForegroundColor Cyan
$nextSteps = @(
  "",
  "Next steps:",
  "  1. Put a GGUF (or a folder with a GGUF + mmproj) into the models folder,",
  "     then add a matching entry to $cfg (see docs/engine-setup.md).",
  "  2. Start everything:  .\launch-windows.ps1   (auto-starts llama-swap)",
  "  3. In the app: Admin -> Models -> add an endpoint at http://127.0.0.1:9090/v1",
  "     (mark supports-tools) if it is not already registered.",
  "  4. Run /engine to auto-size each context window to your GPU, and /doctor",
  "     to confirm browser, voice, and coding-agent are green.",
  "",
  "Full guide: docs/engine-setup.md"
)
$nextSteps | ForEach-Object { Write-Host $_ -ForegroundColor Gray }
