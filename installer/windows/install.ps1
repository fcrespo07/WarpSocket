# WarpSocket - Windows installer / bootstrapper
#
# Two ways to run:
#   1. Remote (via irm | iex):
#      irm https://raw.githubusercontent.com/fcrespo07/WarpSocket/main/installer/windows/install.ps1 | iex
#   2. Local (from a clone):
#      powershell -ExecutionPolicy Bypass -File installer\windows\install.ps1
#
# Environment overrides:
#   $env:WARPSOCKET_COMPONENT  = server|client     Skip the interactive prompt
#   $env:WARPSOCKET_REPO_DIR   = C:\path\to\clone  Use existing repo instead of cloning
#   $env:WARPSOCKET_RUN_WIZARD = 0                 Skip the setup wizard (server only)
#   $env:WSTUNNEL_VERSION      = v10.5.2           Pin a specific wstunnel release
#
# Requires: Windows 10/11, PowerShell 5.1+, Administrator privileges

$ErrorActionPreference = 'Stop'

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
$WARPSOCKET_DIR            = Join-Path $env:ProgramFiles "WarpSocket"
$INSTALL_PREFIX            = Join-Path $WARPSOCKET_DIR "server"
$CLIENT_PREFIX             = Join-Path $WARPSOCKET_DIR "client"
$WSTUNNEL_BIN              = Join-Path $WARPSOCKET_DIR "wstunnel.exe"
$WG_EXE                    = 'C:\Program Files\WireGuard\wireguard.exe'
$DEFAULT_REPO_DIR          = Join-Path $env:USERPROFILE "WarpSocket"
$GITHUB_REPO               = "fcrespo07/WarpSocket"
$GITHUB_REPO_URL           = "https://github.com/$GITHUB_REPO"
$WSTUNNEL_FALLBACK_VERSION = "v10.5.2"
$PYTHON_FALLBACK_VERSION   = "3.12.8"   # used when winget is not available

# ---------------------------------------------------------------------------
# UI helpers
# ---------------------------------------------------------------------------
function Write-Info { param([string]$Msg) Write-Host "==> $Msg" -ForegroundColor Cyan }
function Write-OK   { param([string]$Msg) Write-Host "  [OK] $Msg" -ForegroundColor Green }
function Write-Warn { param([string]$Msg) Write-Host "  [!]  $Msg" -ForegroundColor Yellow }

function Write-Fail {
    param([string]$Msg)
    Write-Host "  [X]  $Msg" -ForegroundColor Red
    exit 1
}

function Show-Banner {
    $art = @'

   _      __                  ____           __        __
  | | /| / /__ _ _____  ___  / __/__  ____  / /_____  / /_
  | |/ |/ / _ `/ __/ _ \/ _ \_\ \/ _ \/ __/ /  '_/ -_) __/
  |__/|__/\_,_/_/  \_,_/ .__/___/\___/\__/_/\_\\__/\__/
                       /_/

'@
    Write-Host $art -ForegroundColor Cyan
    Write-Host "  WireGuard over WebSocket - Windows installer" -ForegroundColor DarkGray
    Write-Host ""
}

# ---------------------------------------------------------------------------
# Elevation check
# ---------------------------------------------------------------------------
function Assert-Admin {
    $identity  = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = [Security.Principal.WindowsPrincipal]::new($identity)
    if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        Write-Fail "This installer requires Administrator. Re-run from an elevated PowerShell prompt."
    }
    Write-OK "Running as Administrator"
}

# ---------------------------------------------------------------------------
# Python 3.11+
# ---------------------------------------------------------------------------
$script:PYTHON_BIN = $null

function Test-PythonExe {
    # Returns the resolved sys.executable if the candidate is a usable Python >=3.11,
    # otherwise $null. The candidate may be a Microsoft Store App Execution Alias —
    # if Python is actually installed those aliases resolve to the real interpreter,
    # so we invoke them instead of pre-filtering by path. Stubs without a real
    # Python behind them write nothing to stdout and are skipped via the output
    # check below.
    param([string]$Exe, [string[]]$ExtraArgs = @())
    if (-not $Exe) { return $null }
    # Use single-quoted f-string inside Python so the whole code can live in a
    # PowerShell double-quoted string without quote-escaping pitfalls.
    $code = "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor};{sys.executable}')"
    $prev = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $out = & $Exe @ExtraArgs -c $code 2>$null
    } catch {
        return $null
    } finally {
        $ErrorActionPreference = $prev
    }
    if ($LASTEXITCODE -ne 0 -or -not $out) { return $null }
    $line = ($out | Select-Object -First 1).ToString().Trim()
    if ($line -notmatch '^(\d+)\.(\d+);(.+)$') { return $null }
    if ([int]$Matches[1] -ne 3 -or [int]$Matches[2] -lt 11) { return $null }
    $resolved = $Matches[3].Trim()
    if (-not (Test-Path $resolved)) { return $null }
    return $resolved
}

function Find-Python311 {
    # 1) Prefer the py.exe launcher (canonical on Windows). Try newest first.
    $pyLauncher = Get-Command py -ErrorAction SilentlyContinue
    if ($pyLauncher) {
        foreach ($flag in '-3.13', '-3.12', '-3.11', '-3') {
            $resolved = Test-PythonExe -Exe $pyLauncher.Source -ExtraArgs @($flag)
            if ($resolved) { $script:PYTHON_BIN = $resolved; return $true }
        }
    }

    # 2) Fall back to PATH lookups, skipping the Store stub.
    foreach ($name in 'python3.13', 'python3.12', 'python3.11', 'python3', 'python') {
        $cmd = Get-Command $name -ErrorAction SilentlyContinue
        if (-not $cmd) { continue }
        $resolved = Test-PythonExe -Exe $cmd.Source
        if ($resolved) { $script:PYTHON_BIN = $resolved; return $true }
    }
    return $false
}

function Refresh-SessionPath {
    $env:PATH = [System.Environment]::GetEnvironmentVariable('PATH', 'Machine') + ';' +
                [System.Environment]::GetEnvironmentVariable('PATH', 'User')
}

function Install-Python-Direct {
    # Fallback when winget is not available: download the MSI from python.org.
    $arch = switch ($env:PROCESSOR_ARCHITECTURE) {
        'AMD64' { 'amd64' }
        'ARM64' { 'arm64' }
        default { 'amd64' }
    }
    $pyInstaller = "python-${PYTHON_FALLBACK_VERSION}-${arch}.exe"
    $pyUrl       = "https://www.python.org/ftp/python/${PYTHON_FALLBACK_VERSION}/${pyInstaller}"
    $tmpPath     = Join-Path $env:TEMP $pyInstaller

    Write-Info "Downloading Python $PYTHON_FALLBACK_VERSION from python.org..."
    Invoke-WebRequest -Uri $pyUrl -OutFile $tmpPath -UseBasicParsing

    Write-Info "Running Python installer (silent, system-wide)..."
    $proc = Start-Process -FilePath $tmpPath `
        -ArgumentList '/quiet', 'InstallAllUsers=1', 'PrependPath=1', 'Include_test=0' `
        -Wait -PassThru
    Remove-Item $tmpPath -ErrorAction SilentlyContinue

    if ($proc.ExitCode -ne 0) {
        Write-Fail "Python installer exited with code $($proc.ExitCode). Install manually from https://python.org and retry."
    }
}

function Install-Python {
    if (Get-Command winget -ErrorAction SilentlyContinue) {
        Write-Info "Installing Python 3.12 via winget..."
        & winget install --id Python.Python.3.12 --source winget --silent --accept-package-agreements --accept-source-agreements
    } else {
        Write-Warn "winget not found - falling back to direct download from python.org"
        Install-Python-Direct
    }
    Refresh-SessionPath
    if (-not (Find-Python311)) {
        Write-Fail "Python 3.11+ install failed. Install manually from https://python.org and retry."
    }
}

function Ensure-Python {
    if (Find-Python311) {
        Write-OK "Python: $($script:PYTHON_BIN) ($( & $script:PYTHON_BIN --version))"
    } else {
        Write-Warn "Python 3.11+ not found - installing"
        Install-Python
        Write-OK "Python installed: $($script:PYTHON_BIN)"
    }
}

# ---------------------------------------------------------------------------
# wstunnel binary
# ---------------------------------------------------------------------------
function Get-LatestWstunnelVersion {
    try {
        $resp = Invoke-RestMethod -Uri "https://api.github.com/repos/erebe/wstunnel/releases/latest" `
                                  -TimeoutSec 15 -UseBasicParsing
        return $resp.tag_name
    } catch {
        Write-Warn "GitHub API query failed: $($_.Exception.Message)"
        return $null
    }
}

function Ensure-Wstunnel {
    if (Test-Path $WSTUNNEL_BIN) {
        $ver = (& $WSTUNNEL_BIN --version 2>&1) | Select-Object -First 1
        Write-OK "wstunnel: $ver"
        return
    }

    Write-Info "Installing wstunnel..."

    $version = $env:WSTUNNEL_VERSION
    if (-not $version) {
        Write-Info "Querying GitHub for latest wstunnel release"
        $version = Get-LatestWstunnelVersion
        if (-not $version) {
            Write-Warn "Could not determine latest version - falling back to $WSTUNNEL_FALLBACK_VERSION"
            $version = $WSTUNNEL_FALLBACK_VERSION
        }
    }
    Write-OK "wstunnel version: $version"

    # Match the naming used by wstunnel's GitHub releases (Go-style: amd64/arm64,
    # same convention as the Linux installer).
    $arch = switch ($env:PROCESSOR_ARCHITECTURE) {
        'AMD64' { 'amd64' }
        'ARM64' { 'arm64' }
        default { Write-Fail "Unsupported architecture: $($env:PROCESSOR_ARCHITECTURE)" }
    }

    $vnum       = $version.TrimStart('v')
    $tarballName = "wstunnel_${vnum}_windows_${arch}.tar.gz"
    $url         = "https://github.com/erebe/wstunnel/releases/download/$version/$tarballName"

    $tmpDir     = Join-Path ([System.IO.Path]::GetTempPath()) ([System.IO.Path]::GetRandomFileName())
    New-Item -ItemType Directory -Path $tmpDir -Force | Out-Null
    $tarballPath = Join-Path $tmpDir $tarballName

    Write-Info "Downloading $url (~5 MB)"
    Invoke-WebRequest -Uri $url -OutFile $tarballPath -UseBasicParsing

    # tar.exe is bundled with Windows 10 1803+ / Windows 11. wstunnel only
    # ships .tar.gz for Windows (no .zip variant).
    if (-not (Get-Command tar -ErrorAction SilentlyContinue)) {
        Write-Fail "tar.exe not found. Requires Windows 10 1803+ or Windows 11."
    }
    & tar -xzf $tarballPath -C $tmpDir
    if ($LASTEXITCODE -ne 0) { Write-Fail "Failed to extract $tarballName (tar exit code $LASTEXITCODE)" }

    New-Item -ItemType Directory -Path $WARPSOCKET_DIR -Force | Out-Null

    $wstunnelExe = Get-ChildItem -Path $tmpDir -Filter 'wstunnel.exe' -Recurse | Select-Object -First 1
    if (-not $wstunnelExe) { Write-Fail "wstunnel.exe not found in downloaded archive" }
    Copy-Item $wstunnelExe.FullName -Destination $WSTUNNEL_BIN
    Remove-Item -Recurse -Force $tmpDir

    Add-ToSystemPath $WARPSOCKET_DIR
    Write-OK "wstunnel installed: $WSTUNNEL_BIN"
}

# ---------------------------------------------------------------------------
# PATH helper
# ---------------------------------------------------------------------------
function Add-ToSystemPath {
    param([string]$Dir)
    $current = [System.Environment]::GetEnvironmentVariable('PATH', 'Machine')
    $dirs    = $current -split ';' | Where-Object { $_.Trim() }
    if ($Dir -notin $dirs) {
        [System.Environment]::SetEnvironmentVariable('PATH', "$current;$Dir", 'Machine')
        $env:PATH += ";$Dir"
        Write-OK "Added $Dir to system PATH"
    }
}

# ---------------------------------------------------------------------------
# Repo location
# ---------------------------------------------------------------------------
$script:REPO_DIR = $null

function Resolve-Repo {
    # 1. Env override
    if ($env:WARPSOCKET_REPO_DIR) {
        $script:REPO_DIR = $env:WARPSOCKET_REPO_DIR
        if (-not (Test-Path $script:REPO_DIR)) {
            Write-Fail "WARPSOCKET_REPO_DIR=$($script:REPO_DIR) does not exist"
        }
        Write-OK "Using repo at: $($script:REPO_DIR)"
        return
    }

    # 2. Running from inside a clone (local -File usage)
    if ($PSScriptRoot) {
        $resolved = Resolve-Path (Join-Path $PSScriptRoot '..\..')  -ErrorAction SilentlyContinue
        if ($resolved) {
            $candidate = $resolved.Path
            if ((Test-Path (Join-Path $candidate 'CLAUDE.md')) -and
                (Test-Path (Join-Path $candidate 'server'))) {
                $script:REPO_DIR = $candidate
                Write-OK "Using local checkout: $($script:REPO_DIR)"
                return
            }
        }
    }

    # 3. Clone
    Write-Info "Repository not found locally - cloning"

    if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
        if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
            Write-Fail "git and winget not found. Install Git from https://git-scm.com and retry."
        }
        Write-Info "Installing Git via winget..."
        & winget install --id Git.Git --silent --accept-package-agreements --accept-source-agreements
        Refresh-SessionPath
    }

    $script:REPO_DIR = $DEFAULT_REPO_DIR
    if (Test-Path $script:REPO_DIR) {
        Write-Warn "Directory $($script:REPO_DIR) already exists - using as-is"
        return
    }

    $ghCmd = Get-Command gh -ErrorAction SilentlyContinue
    if ($ghCmd) {
        & gh auth status 2>&1 | Out-Null
        if ($LASTEXITCODE -eq 0) {
            Write-Info "Cloning via gh (authenticated)"
            & gh repo clone $GITHUB_REPO $script:REPO_DIR
            Write-OK "Cloned to: $($script:REPO_DIR)"
            return
        }
    }

    Write-Warn "gh not authenticated - trying HTTPS clone (will fail fast if repo is private)"
    $env:GIT_TERMINAL_PROMPT = '0'
    & git clone $GITHUB_REPO_URL $script:REPO_DIR 2>&1 | ForEach-Object { Write-Host $_ }
    if ($LASTEXITCODE -ne 0) {
        Remove-Item Env:GIT_TERMINAL_PROMPT -ErrorAction SilentlyContinue
        Write-Fail @"
git clone failed. If the repo is private, either:
  1) Install + auth gh:  gh auth login  and re-run this installer
  2) Clone manually then re-run with:
       `$env:WARPSOCKET_REPO_DIR = 'C:\path\to\WarpSocket'; .\install.ps1
"@
    }
    Remove-Item Env:GIT_TERMINAL_PROMPT -ErrorAction SilentlyContinue
    Write-OK "Cloned to: $($script:REPO_DIR)"
}

# ---------------------------------------------------------------------------
# Venv helper (validates and recreates if broken)
# ---------------------------------------------------------------------------
function Ensure-Venv {
    param([string]$Prefix, [string]$Label)
    $pip = Join-Path $Prefix '.venv\Scripts\pip.exe'
    if (-not (Test-Path $pip)) {
        $venvDir = Join-Path $Prefix '.venv'
        if (Test-Path $venvDir) {
            Write-Warn "Existing $Label venv is broken - recreating"
            Remove-Item -Recurse -Force $venvDir
        }
        Write-Info "Creating $Label venv with $($script:PYTHON_BIN)"
        & $script:PYTHON_BIN -m venv $venvDir
    } else {
        Write-OK "$Label venv already exists - reusing"
    }
}

# ---------------------------------------------------------------------------
# Server install
# ---------------------------------------------------------------------------
function Install-Server {
    Write-Info "Installing WarpSocket server to $INSTALL_PREFIX"

    if (-not (Test-Path (Join-Path $script:REPO_DIR 'server'))) {
        Write-Fail "Server source not found at $($script:REPO_DIR)\server"
    }

    New-Item -ItemType Directory -Path $INSTALL_PREFIX -Force | Out-Null
    Ensure-Venv -Prefix $INSTALL_PREFIX -Label 'server'

    $pip = Join-Path $INSTALL_PREFIX '.venv\Scripts\pip.exe'
    Write-Info "Installing Python dependencies (this may take ~30-60s)"
    & $pip install --upgrade --disable-pip-version-check pip
    & $pip install --disable-pip-version-check -e (Join-Path $script:REPO_DIR 'server')

    # Shim in $WARPSOCKET_DIR (which is in PATH)
    $exePath  = Join-Path $INSTALL_PREFIX '.venv\Scripts\warpsocket-server.exe'
    $shimPath = Join-Path $WARPSOCKET_DIR 'warpsocket-server.bat'
    "@echo off`r`n`"$exePath`" %*" | Out-File -FilePath $shimPath -Encoding ascii
    Write-OK "warpsocket-server -> $exePath"

    Write-OK "Server installed"
}

function Invoke-SetupWizard {
    if ($env:WARPSOCKET_RUN_WIZARD -eq '0') {
        Write-Info "Skipping setup wizard (WARPSOCKET_RUN_WIZARD=0)"
        Write-Host ""
        Write-Host "  Run the wizard later with:"
        Write-Host "    warpsocket-server setup"
        Write-Host ""
        return
    }
    Write-Host ""
    Write-Info "Launching the setup wizard..."
    Write-Host ""
    $exePath = Join-Path $INSTALL_PREFIX '.venv\Scripts\warpsocket-server.exe'
    & $exePath setup
}

# ---------------------------------------------------------------------------
# Client install
# ---------------------------------------------------------------------------
function Install-Client {
    Write-Info "Installing WarpSocket client to $CLIENT_PREFIX"

    if (-not (Test-Path (Join-Path $script:REPO_DIR 'client'))) {
        Write-Fail "Client source not found at $($script:REPO_DIR)\client"
    }

    # WireGuard for Windows
    if (Test-Path $WG_EXE) {
        Write-OK "WireGuard for Windows: found"
    } else {
        if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
            Write-Fail "winget not found and WireGuard is missing. Install from https://www.wireguard.com/install/ and retry."
        }
        Write-Info "Installing WireGuard for Windows via winget..."
        & winget install --id WireGuard.WireGuard --silent --accept-package-agreements --accept-source-agreements
        if (-not (Test-Path $WG_EXE)) {
            Write-Fail "WireGuard install failed. Install from https://www.wireguard.com/install/ and retry."
        }
        Write-OK "WireGuard installed"
    }

    New-Item -ItemType Directory -Path $CLIENT_PREFIX -Force | Out-Null
    Ensure-Venv -Prefix $CLIENT_PREFIX -Label 'client'

    $pip = Join-Path $CLIENT_PREFIX '.venv\Scripts\pip.exe'
    Write-Info "Installing Python dependencies (this may take ~30-60s)"
    & $pip install --upgrade --disable-pip-version-check pip
    & $pip install --disable-pip-version-check -e (Join-Path $script:REPO_DIR 'client')

    # Shim in $WARPSOCKET_DIR (in PATH)
    $exePath  = Join-Path $CLIENT_PREFIX '.venv\Scripts\warpsocket.exe'
    $shimPath = Join-Path $WARPSOCKET_DIR 'warpsocket.bat'
    "@echo off`r`n`"$exePath`" %*" | Out-File -FilePath $shimPath -Encoding ascii
    Write-OK "warpsocket -> $exePath"

    # Startup shortcut (launch tray at login)
    $startupDir   = Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs\Startup'
    $iconFile     = Join-Path $script:REPO_DIR 'client\warpsocket\resources\app_icon.ico'
    $wsh          = New-Object -ComObject WScript.Shell

    $startupSc = $wsh.CreateShortcut((Join-Path $startupDir 'WarpSocket.lnk'))
    $startupSc.TargetPath       = $exePath
    $startupSc.WorkingDirectory = Split-Path $exePath
    $startupSc.WindowStyle      = 7  # minimized (tray apps start without a visible window)
    if (Test-Path $iconFile) { $startupSc.IconLocation = $iconFile }
    $startupSc.Save()
    Write-OK "Startup shortcut: $(Join-Path $startupDir 'WarpSocket.lnk')"

    # Desktop shortcut
    $desktopDir = $wsh.SpecialFolders('Desktop')
    $desktopSc  = $wsh.CreateShortcut((Join-Path $desktopDir 'WarpSocket.lnk'))
    $desktopSc.TargetPath       = $exePath
    $desktopSc.WorkingDirectory = Split-Path $exePath
    if (Test-Path $iconFile) { $desktopSc.IconLocation = $iconFile }
    $desktopSc.Save()
    Write-OK "Desktop shortcut: $(Join-Path $desktopDir 'WarpSocket.lnk')"

    Write-Host ""
    Write-Host "  Client installed." -ForegroundColor Green
    Write-Host ""
    Write-Host "  Next steps:"
    Write-Host "    1. Drop your .warpcfg file somewhere accessible."
    Write-Host "    2. Launch WarpSocket from the desktop shortcut or run: warpsocket"
    Write-Host "    3. The first run opens the import wizard to load your .warpcfg."
    Write-Host ""
    Write-Host "  Startup entry : $(Join-Path $startupDir 'WarpSocket.lnk')"
    Write-Host "  Desktop       : $(Join-Path $desktopDir 'WarpSocket.lnk')"
    Write-Host ""
}

# ---------------------------------------------------------------------------
# Component picker
# ---------------------------------------------------------------------------
function Select-Component {
    $component = $env:WARPSOCKET_COMPONENT

    if (-not $component) {
        Write-Host ""
        Write-Host "Which component do you want to install?" -ForegroundColor White
        Write-Host "  1) Server  - runs wstunnel + WireGuard, accepts client connections"
        Write-Host "  2) Client  - connects to a WarpSocket server"
        Write-Host ""

        while (-not $component) {
            $choice = Read-Host "Choice [1/2] (default 1)"
            if (-not $choice) { $choice = '1' }
            switch ($choice.ToLower()) {
                { $_ -in '1', 'server' } { $component = 'server' }
                { $_ -in '2', 'client' } { $component = 'client' }
                default                  { Write-Host "  Invalid choice - pick 1 or 2" }
            }
        }
    }

    switch ($component) {
        'server' { Install-Server; Invoke-SetupWizard }
        'client' { Install-Client }
        default  { Write-Fail "Unknown component: $component (must be 'server' or 'client')" }
    }
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
function Main {
    Show-Banner
    Assert-Admin
    Write-Info "Preparing system dependencies"
    Ensure-Python
    Ensure-Wstunnel
    Resolve-Repo
    Select-Component
    Write-Host ""
    Write-OK "All done."
}

Main
