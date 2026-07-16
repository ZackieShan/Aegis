#Requires -Version 5.1
<#
  Aegis - native Windows launcher (no Docker).

  One command to: create a virtualenv, install dependencies, run first-time
  setup (default login: admin / admin), and start the server.
  Safe to re-run - it skips whatever already exists.

  Usage (via the .bat wrapper, which bypasses the PowerShell execution policy):
    .\launch-windows.bat
    .\launch-windows.bat -Port 7000 -BindHost 127.0.0.1

  Tip: bind 127.0.0.1 (default) for local-only use. Use 0.0.0.0 only when you
  intentionally want other devices on your LAN to reach it.
#>
param(
    [int]$Port = 7000,
    [string]$BindHost = "127.0.0.1",
    # Open the app in the default browser once the server answers. Used by the
    # Start Menu shortcut so double-clicking the icon feels like opening an app.
    [switch]$OpenBrowser
)

$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

# Force Python's UTF-8 mode: the codebase reads/writes UTF-8 files, and
# Windows' default locale encoding (cp1252) breaks them. On Linux/macOS
# UTF-8 is already the default, so this makes both platforms behave alike.
$env:PYTHONUTF8 = "1"

function Write-Step($msg) { Write-Host ""; Write-Host ("==> " + $msg) -ForegroundColor Cyan }
function Fail($msg) {
    Write-Host ""
    Write-Host ("ERROR: " + $msg) -ForegroundColor Red
    Write-Host ""
    Read-Host "Press Enter to exit"
    exit 1
}

function Test-WindowsBashStub($path) {
    if (-not $path) { return $false }
    $lowered = $path.ToLowerInvariant()
    foreach ($stub in @("system32\bash.exe", "sysnative\bash.exe", "windowsapps\bash.exe")) {
        if ($lowered.Contains($stub)) { return $true }
    }
    return $false
}

function Find-GitBash {
    $cmd = Get-Command bash -ErrorAction SilentlyContinue
    if ($cmd -and -not (Test-WindowsBashStub $cmd.Source)) { return $cmd.Source }

    $roots = @()
    foreach ($name in @("ProgramFiles", "ProgramW6432", "ProgramFiles(x86)", "LocalAppData")) {
        $base = [Environment]::GetEnvironmentVariable($name)
        if ($base) {
            $roots += (Join-Path $base "Git")
            if ($name -eq "LocalAppData") { $roots += (Join-Path $base "Programs\Git") }
        }
    }
    $roots += @("C:\Program Files\Git", "C:\Program Files (x86)\Git")

    foreach ($root in ($roots | Select-Object -Unique)) {
        foreach ($relative in @("bin\bash.exe", "usr\bin\bash.exe")) {
            $candidate = Join-Path $root $relative
            if (Test-Path $candidate) { return $candidate }
        }
    }
    return $null
}

# 1. Locate a Python interpreter (3.11+ required)
Write-Step "Checking for Python"
function Get-PythonVersionText($launcher, $launcherArgs) {
    try {
        return (& $launcher @launcherArgs -c "import sys; print('.'.join(map(str, sys.version_info[:3])))" 2>$null).Trim()
    } catch {
        return $null
    }
}

$pyExe = $null
$pyArgs = @()
$pyVersion = $null

$pyLauncher = Get-Command py -ErrorAction SilentlyContinue
if ($pyLauncher) {
    foreach ($v in @("-3.13", "-3.12", "-3.11")) {
        $ver = Get-PythonVersionText $pyLauncher.Source @($v)
        if ($ver) {
            $pyExe = $pyLauncher.Source
            $pyArgs = @($v)
            $pyVersion = $ver
            break
        }
    }
}

if (-not $pyExe) {
    $pythonCmd = Get-Command python -ErrorAction SilentlyContinue
    if ($pythonCmd) {
        $ver = Get-PythonVersionText $pythonCmd.Source @()
        if ($ver) {
            $versionParts = $ver.Split('.')
            $major = [int]$versionParts[0]
            $minor = [int]$versionParts[1]
            if ($major -gt 3 -or ($major -eq 3 -and $minor -ge 11)) {
                $pyExe = $pythonCmd.Source
                $pyVersion = $ver
            }
        }
    }
}

if ($pyExe -like "*WindowsApps*python.exe") {
    $pyCmd = Get-Command py -ErrorAction SilentlyContinue
    if ($pyCmd) {
        $pyExe = $pyCmd.Source
        $pyArgs = @("-3.11")
    }
}

if (-not $pyExe) {
    Fail "Couldn't find Python 3.11+ for Windows setup. Install Python 3.11+ (or open the Python launcher with 'py -3.11') from https://www.python.org/downloads/, then re-run this script."
}
$pythonLabel = ("Using Python {0}: {1} {2}" -f $pyVersion, $pyExe, ($pyArgs -join ' ')).TrimEnd()
Write-Host $pythonLabel

# 2. Create the virtualenv if missing
$venvPy = Join-Path $PSScriptRoot "venv\Scripts\python.exe"
if (-not (Test-Path $venvPy)) {
    Write-Step "Creating virtual environment (venv)"
    & $pyExe @pyArgs -m venv venv
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path $venvPy)) { Fail "Failed to create the virtual environment." }
} else {
    Write-Host "venv already exists - skipping creation."
}

# 3. Install / update dependencies. Skipped when requirements.txt is unchanged
#    since the last successful install (hash marker in the venv), so day-to-day
#    starts are fast — mirrors start-linux.sh / start-macos.sh.
$reqHash = (Get-FileHash -Algorithm MD5 -Path "requirements.txt").Hash
$reqHashFile = Join-Path $PSScriptRoot "venv\.requirements_hash"
$prevHash = if (Test-Path $reqHashFile) { (Get-Content $reqHashFile -TotalCount 1).Trim() } else { "" }
if ($reqHash -ne $prevHash) {
    Write-Step "Installing dependencies (first run can take a few minutes)"
    & $venvPy -m pip install --upgrade pip --quiet
    & $venvPy -m pip install -r requirements.txt
    if ($LASTEXITCODE -ne 0) { Fail "Dependency install failed. Scroll up for the pip error." }
    Set-Content -Path $reqHashFile -Value $reqHash -Encoding ascii
} else {
    Write-Step "Dependencies up to date - skipping install"
}

# requirements.txt ships chromadb-client (thin HTTP client for the Docker
# Chroma container). Native runs start their OWN local Chroma server from the
# venv (step 6b) - which needs the FULL chromadb package. Swap it in once, the
# same way start-linux.sh / start-macos.sh do. Marker file keeps re-runs fast.
$chromaMarker = Join-Path $PSScriptRoot "venv\.chromadb_full"
if (-not (Test-Path $chromaMarker)) {
    if (& $venvPy -m pip show chromadb-client 2>$null) {
        Write-Step "Installing full ChromaDB for local vector search"
        & $venvPy -m pip uninstall -y chromadb-client | Out-Null
        & $venvPy -m pip install chromadb
        if ($LASTEXITCODE -ne 0) { Fail "ChromaDB install failed. Scroll up for the pip error." }
    }
    if (& $venvPy -m pip show chromadb 2>$null) { Set-Content -Path $chromaMarker -Value "1" -Encoding ascii }
}

# 4. First-time setup (creates data dirs, DB, .env, admin user)
Write-Step "Running first-time setup"
& $venvPy setup.py
if ($LASTEXITCODE -ne 0) { Fail "setup.py failed." }

# 5. Friendly note about Git Bash (full Cookbook / agent-shell parity)
if (-not (Find-GitBash)) {
    Write-Host ""
    Write-Host "NOTE: Git Bash (bash.exe) was not found on PATH." -ForegroundColor Yellow
    Write-Host "      The core app works without it. For full Cookbook background" -ForegroundColor Yellow
    Write-Host "      downloads and the agent shell tool, install Git for Windows:" -ForegroundColor Yellow
    Write-Host "      https://git-scm.com/download/win" -ForegroundColor Yellow
}

# 6. Point CUDA_PATH at a real CUDA toolkit so GPU llama-cpp-python can import.
$cudaBase = "C:\Program Files\NVIDIA GPU Computing Toolkit\CUDA"
if (Test-Path $cudaBase) {
    $cudaBest = Get-ChildItem $cudaBase -Directory -ErrorAction SilentlyContinue |
        Where-Object { Test-Path (Join-Path $_.FullName "bin") } |
        Sort-Object { try { [version]($_.Name -replace "^v", "") } catch { [version]"0.0" } } -Descending |
        Select-Object -First 1
    if ($cudaBest) {
        $env:CUDA_PATH = $cudaBest.FullName
        Write-Host ("Using CUDA_PATH = " + $cudaBest.FullName) -ForegroundColor Cyan
    }
}

# 6b. ChromaDB backs vector RAG, semantic memory, and the tool index. Start a
#     local server from the venv unless one is already reachable, or the app is
#     configured to use a remote/Docker Chroma (CHROMADB_HOST set to something
#     other than localhost). Matches start-linux.sh / start-macos.sh.
$chromaProc = $null
$chromaHost = if ($env:CHROMADB_HOST) { $env:CHROMADB_HOST } else { "localhost" }
$chromaPort = if ($env:CHROMADB_PORT) { $env:CHROMADB_PORT } else { "8100" }
function Test-PortOpen($h, $p) {
    try { $c = New-Object Net.Sockets.TcpClient; $c.Connect($h, [int]$p); $c.Close(); return $true }
    catch { return $false }
}
if (Test-PortOpen "127.0.0.1" $chromaPort) {
    Write-Host ("ChromaDB already running on 127.0.0.1:{0} - using it." -f $chromaPort) -ForegroundColor Cyan
} elseif ($chromaHost -notin @("localhost", "127.0.0.1")) {
    Write-Host ("CHROMADB_HOST={0} is remote - not starting a local ChromaDB." -f $chromaHost)
} else {
    $chromaExe = Join-Path $PSScriptRoot "venv\Scripts\chroma.exe"
    if (Test-Path $chromaExe) {
        $chromaLog = Join-Path $env:TEMP "aegis-chromadb.log"
        Write-Step ("Starting ChromaDB in the background on 127.0.0.1:{0}" -f $chromaPort)
        Write-Host ("  logging to {0}" -f $chromaLog)
        $chromaDataPath = Join-Path $PSScriptRoot "data\chroma"
        $chromaProc = Start-Process -FilePath $chromaExe `
            -ArgumentList @("run", "--host", "127.0.0.1", "--port", "$chromaPort", "--path", $chromaDataPath) `
            -WindowStyle Hidden -RedirectStandardOutput $chromaLog -RedirectStandardError "$chromaLog.err" -PassThru
    } else {
        Write-Host "ChromaDB CLI not found in venv; vector search will be degraded (keyword fallback)." -ForegroundColor Yellow
    }
}

# Stop the background ChromaDB when this launcher exits (Ctrl+C / window close).
if ($chromaProc) {
    $stopChroma = { if ($chromaProc -and -not $chromaProc.HasExited) { try { $chromaProc.Kill() } catch {} } }.GetNewClosure()
    Register-EngineEvent -SourceIdentifier PowerShell.Exiting -Action $stopChroma | Out-Null
}

# 6c. Ollama is the easiest local-model backend on Windows (bundles a GPU
#     llama.cpp engine — no build step). If it's installed but its server isn't
#     up, start it so its models are reachable the moment Aegis launches. If it
#     was already running (or is the desktop app's service), leave it be — we
#     only stop what WE started, so the user's service isn't touched.
$ollamaProc = $null
if (Test-PortOpen "127.0.0.1" 11434) {
    Write-Host "Ollama already running on 127.0.0.1:11434 - using it." -ForegroundColor Cyan
} else {
    $ollamaCmd = Get-Command ollama -ErrorAction SilentlyContinue
    if ($ollamaCmd) {
        $ollamaLog = Join-Path $env:TEMP "aegis-ollama.log"
        Write-Step "Starting Ollama in the background"
        $ollamaProc = Start-Process -FilePath $ollamaCmd.Source -ArgumentList @("serve") `
            -WindowStyle Hidden -RedirectStandardOutput $ollamaLog -RedirectStandardError "$ollamaLog.err" -PassThru
    } else {
        Write-Host "Ollama not installed - install it from https://ollama.com/download for one-click local models." -ForegroundColor Yellow
    }
}
if ($ollamaProc) {
    $stopOllama = { if ($ollamaProc -and -not $ollamaProc.HasExited) { try { $ollamaProc.Kill() } catch {} } }.GetNewClosure()
    Register-EngineEvent -SourceIdentifier PowerShell.Exiting -Action $stopOllama | Out-Null
}

# 6d. llama-swap fronts a CUDA llama.cpp server: it serves the raw GGUFs in
#     models/ over an OpenAI API with on-demand model swap and grammar-locked
#     native tool calls (far more reliable than Ollama for tool use). It's
#     optional — if the engine binaries aren't present we skip cleanly and
#     Ollama still works. Set AEGIS_ENGINE_DIR to override the location.
$swapProc = $null
$engineDir = if ($env:AEGIS_ENGINE_DIR) { $env:AEGIS_ENGINE_DIR } else { Join-Path $PSScriptRoot "..\engine" }
$swapExe   = Join-Path $engineDir "llama-swap\llama-swap.exe"
$swapCfg   = Join-Path $engineDir "llama-swap.yaml"
$swapPort  = if ($env:LLAMA_SWAP_PORT) { $env:LLAMA_SWAP_PORT } else { "9090" }
if (Test-PortOpen "127.0.0.1" $swapPort) {
    Write-Host ("llama-swap already running on 127.0.0.1:{0} - using it." -f $swapPort) -ForegroundColor Cyan
} elseif ((Test-Path $swapExe) -and (Test-Path $swapCfg)) {
    $swapLog = Join-Path $env:TEMP "aegis-llama-swap.log"
    Write-Step ("Starting llama-swap in the background on 127.0.0.1:{0}" -f $swapPort)
    # -watch-config: llama-swap hot-reloads the YAML on change, so Aegis's
    # context auto-tuner (/engine) can resize model windows without a restart.
    $swapProc = Start-Process -FilePath $swapExe `
        -ArgumentList @("-config", $swapCfg, "-watch-config", "-listen", ("127.0.0.1:{0}" -f $swapPort)) `
        -WindowStyle Hidden -RedirectStandardOutput $swapLog -RedirectStandardError "$swapLog.err" -PassThru
} else {
    Write-Host ("llama-swap engine not found at {0} - skipping (Ollama still available)." -f $engineDir) -ForegroundColor DarkGray
}
if ($swapProc) {
    $stopSwap = { if ($swapProc -and -not $swapProc.HasExited) { try { $swapProc.Kill() } catch {} } }.GetNewClosure()
    Register-EngineEvent -SourceIdentifier PowerShell.Exiting -Action $stopSwap | Out-Null
}

# 6e. ComfyUI is the second media engine (workflows sd.cpp can't run: GGUF
#     video models via custom nodes, Wan-Animate, node ecosystem). Optional —
#     started only when engine/comfyui exists. Aegis frees its VRAM after
#     each job so it coexists with llama-swap on one GPU.
$comfyProc = $null
$comfyDir  = Join-Path $engineDir "comfyui"
$comfyMain = Join-Path $comfyDir "ComfyUI\main.py"
$comfyPy   = Join-Path $comfyDir "venv\Scripts\python.exe"
$comfyPort = if ($env:COMFYUI_PORT) { $env:COMFYUI_PORT } else { "8188" }
if (Test-PortOpen "127.0.0.1" $comfyPort) {
    Write-Host ("ComfyUI already running on 127.0.0.1:{0} - using it." -f $comfyPort) -ForegroundColor Cyan
} elseif ((Test-Path $comfyMain) -and (Test-Path $comfyPy)) {
    $comfyLog = Join-Path $env:TEMP "aegis-comfyui.log"
    Write-Step ("Starting ComfyUI in the background on 127.0.0.1:{0}" -f $comfyPort)
    $comfyProc = Start-Process -FilePath $comfyPy `
        -ArgumentList @($comfyMain, "--listen", "127.0.0.1", "--port", $comfyPort, "--disable-auto-launch") `
        -WorkingDirectory (Split-Path $comfyMain) `
        -WindowStyle Hidden -RedirectStandardOutput $comfyLog -RedirectStandardError "$comfyLog.err" -PassThru
} else {
    Write-Host ("ComfyUI not found at {0} - skipping (sd-server still handles image/video)." -f $comfyDir) -ForegroundColor DarkGray
}
if ($comfyProc) {
    $stopComfy = { if ($comfyProc -and -not $comfyProc.HasExited) { try { $comfyProc.Kill() } catch {} } }.GetNewClosure()
    Register-EngineEvent -SourceIdentifier PowerShell.Exiting -Action $stopComfy | Out-Null
}

# 6f. Put Aegis's portable Node (engine/node) on PATH so npx-based MCP servers
#     (the built-in Browser / Playwright) and the doctor's npx check find it,
#     without requiring a system-wide Node install. Skipped cleanly if absent.
$nodeDir = Join-Path $engineDir "node"
if (Test-Path (Join-Path $nodeDir "node.exe")) {
    if (($env:PATH -split ';') -notcontains $nodeDir) {
        $env:PATH = "$nodeDir;$env:PATH"
    }
    Write-Host ("Using portable Node at {0}" -f $nodeDir) -ForegroundColor DarkGray
}

# 7. Start the server (use `python -m uvicorn` - bare `uvicorn` may not be on PATH)
Write-Step ("Starting Aegis at http://{0}:{1}" -f $BindHost, $Port)
Write-Host "Press Ctrl+C to stop."
Write-Host ""
if ($OpenBrowser) {
    # Detached poller: opens the browser only once the server actually answers,
    # so the user never stares at a connection-refused tab. Detached (not a PS
    # job) because uvicorn blocks this console until shutdown.
    $browseHost = if ($BindHost -eq "0.0.0.0") { "127.0.0.1" } else { $BindHost }
    $pollCmd = "for (`$i=0; `$i -lt 120; `$i++) { try { Invoke-WebRequest -UseBasicParsing -TimeoutSec 2 'http://$browseHost`:$Port/api/health' | Out-Null; Start-Process 'http://$browseHost`:$Port'; break } catch { Start-Sleep -Seconds 2 } }"
    Start-Process powershell -WindowStyle Hidden -ArgumentList @("-NoProfile", "-Command", $pollCmd) | Out-Null
}
& $venvPy -m uvicorn app:app --host $BindHost --port $Port
