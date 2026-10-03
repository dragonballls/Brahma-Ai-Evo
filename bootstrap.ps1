param(
    [switch]$IsElevated = $false
)

# 1. Only elevate when installation/repair work is actually required.
# Normal launches should not trigger UAC. Dependencies are installed into the
# repository venv, so administrative privileges are unnecessary in the healthy path.
$IsAdministrator = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator
)

$ErrorActionPreference = "Stop"
$WorkingDir = Split-Path -Parent $MyInvocation.MyCommand.Definition
Set-Location -Path $WorkingDir

# Runtime versions are owned by core/runtime_contract.py. Read them directly so
# PowerShell cannot silently drift from the Python/packaging contract.
$RuntimeContractPath = Join-Path $WorkingDir "core\runtime_contract.py"
if (-not (Test-Path $RuntimeContractPath)) {
    throw "Missing canonical runtime contract: $RuntimeContractPath"
}
$RuntimeContract = Get-Content -Path $RuntimeContractPath -Raw
function Get-RuntimeContractValue([string]$Name) {
    $pattern = '(?m)^\s*' + [regex]::Escape($Name) + '\s*=\s*"([^"]+)"\s*$'
    $match = [regex]::Match($RuntimeContract, $pattern)
    if (-not $match.Success) {
        throw "Runtime contract value '$Name' is missing."
    }
    return $match.Groups[1].Value
}
$PythonMajorMinor = Get-RuntimeContractValue "PYTHON_MAJOR_MINOR"
$PythonBootstrapVersion = Get-RuntimeContractValue "PYTHON_BOOTSTRAP_VERSION"
$NodeVersion = Get-RuntimeContractValue "NODE_VERSION"

Write-Host "==========================================================================" -ForegroundColor Yellow
Write-Host "  ____  ____      _    _   _ __  __    _      _    ___ " -ForegroundColor Yellow
Write-Host " | __ )|  _ \    / \  | | | |  \/  |  / \    / \  |_ _|" -ForegroundColor Yellow
Write-Host " |  _ \| |_) |  / _ \ | |_| | |\/| | / _ \  / _ \  | | " -ForegroundColor Yellow
Write-Host " | |_) |  _ <  / ___ \|  _  | |  | |/ ___ \/ ___ \ | | " -ForegroundColor Yellow
Write-Host " |____/|_| \_\/_/   \_\_| |_|_|  |_/_/   \_\_/   \_\___|" -ForegroundColor Yellow
Write-Host "                      BRAHMA EVO" -ForegroundColor Cyan
Write-Host "         AUTONOMOUS SELF-EVOLUTION COGNITIVE ENGINE" -ForegroundColor Green
Write-Host "   [Skill Forge // Crucible Sandbox // 180 FPS HoloCore]" -ForegroundColor Cyan
Write-Host "==========================================================================" -ForegroundColor Yellow
Write-Host ""

# 2. Helper to refresh environment variables
function Update-Environment {
    $env:Path = [System.Environment]::GetEnvironmentVariable("Path","Machine") + ";" + [System.Environment]::GetEnvironmentVariable("Path","User")
}

# 3. Check for the canonical Python runtime
$PythonExe = $null
$PythonArgs = @("-$PythonMajorMinor")
$PythonVersionCheck = "import sys; expected=tuple(int(x) for x in '$PythonMajorMinor'.split('.')); raise SystemExit(0 if sys.version_info[:2] == expected else 1)"

if (Get-Command "py" -ErrorAction SilentlyContinue) {
    try {
        & py $PythonArgs[0] -c $PythonVersionCheck 2>$null
        if ($LASTEXITCODE -eq 0) {
            $PythonExe = "py"
        }
    } catch {
        $PythonExe = $null
    }
}

if (-not $PythonExe -and (Get-Command "python" -ErrorAction SilentlyContinue)) {
    try {
        & python -c $PythonVersionCheck 2>$null
        if ($LASTEXITCODE -eq 0) {
            $PythonExe = "python"
            $PythonArgs = @()
        }
    } catch {
        $PythonExe = $null
    }
}

if (-not $PythonExe) {
    Write-Host "Python $PythonMajorMinor not found. Downloading Python $PythonBootstrapVersion..." -ForegroundColor Yellow
    $PythonUrl = "https://www.python.org/ftp/python/$PythonBootstrapVersion/python-$PythonBootstrapVersion-amd64.exe"
    $PythonInstaller = "$env:TEMP\python-$PythonBootstrapVersion-installer.exe"
    Invoke-WebRequest -Uri $PythonUrl -OutFile $PythonInstaller

    Write-Host "Installing Python $PythonBootstrapVersion (Silent Mode)..." -ForegroundColor Yellow
    Start-Process -FilePath $PythonInstaller -ArgumentList "/quiet InstallAllUsers=1 PrependPath=1 Include_test=0" -Wait
    Update-Environment

    $PythonExe = "python"
    $PythonArgs = @()
    try {
        & $PythonExe -c $PythonVersionCheck 2>$null
        if ($LASTEXITCODE -ne 0) {
            throw "Installed Python is not Python $PythonMajorMinor."
        }
    } catch {
        throw "Python $PythonMajorMinor installation could not be verified: $($_.Exception.Message)"
    }
}

$PythonDisplay = if ($PythonArgs.Count) { "$PythonExe $($PythonArgs -join ' ')" } else { $PythonExe }
Write-Host "Using supported Python: $PythonDisplay" -ForegroundColor Green

# 4. Check for the canonical Node.js runtime
if (-not (Get-Command "node" -ErrorAction SilentlyContinue)) {
    Write-Host "Node.js not found. Downloading Node v$NodeVersion..." -ForegroundColor Yellow
    $NodeUrl = "https://nodejs.org/dist/v$NodeVersion/node-v$NodeVersion-x64.msi"
    $NodeInstaller = "$env:TEMP\node-v$NodeVersion-installer.msi"
    Invoke-WebRequest -Uri $NodeUrl -OutFile $NodeInstaller
    
    Write-Host "Installing Node.js v$NodeVersion (Silent Mode)..." -ForegroundColor Yellow
    Start-Process -FilePath "msiexec.exe" -ArgumentList @("/i", $NodeInstaller, "/qn") -Wait
    
    Write-Host "Node.js installed successfully." -ForegroundColor Green
    Update-Environment
} else {
    Write-Host "Node.js is already installed: $(Get-Command node | Select-Object -ExpandProperty Source)" -ForegroundColor Green
}

# 5. Virtual Environment Setup
$VenvDir = Join-Path -Path $WorkingDir -ChildPath ".venv"
$VenvPython = Join-Path -Path $VenvDir -ChildPath "Scripts\python.exe"
$VenvPythonW = Join-Path -Path $VenvDir -ChildPath "Scripts\pythonw.exe"

if (-not (Test-Path $VenvPython)) {
    Write-Host "Creating Virtual Environment in .venv..." -ForegroundColor Cyan
    if (Test-Path $VenvDir) { Remove-Item -Recurse -Force $VenvDir }
    $venvArgs = @()
    $venvArgs += $PythonArgs
    $venvArgs += @("-m", "venv", ".venv")
    Start-Process -FilePath $PythonExe -ArgumentList $venvArgs -Wait -NoNewWindow
    Write-Host "Virtual Environment created." -ForegroundColor Green
} else {
    Write-Host "Virtual Environment already exists." -ForegroundColor Green
}

# 6. Verify/repair dependencies only when the venv cannot import the app.
$NeedsRepair = $false
try {
    & $VenvPython -c "import PyQt6, requests, psutil" | Out-Null
    if ($LASTEXITCODE -ne 0) { $NeedsRepair = $true }
} catch {
    $NeedsRepair = $true
}
if ($NeedsRepair) {
    Write-Host "Brahma dependencies need repair; installing into .venv..." -ForegroundColor Cyan
    if (-not $IsAdministrator) {
        Write-Host "Using the repository-local virtual environment; no elevation is required." -ForegroundColor DarkGray
    }
    & $VenvPython -m pip install -r requirements.txt
    if ($LASTEXITCODE -ne 0) {
        throw "Dependency installation failed."
    }
    try {
        & $VenvPython -m playwright install
    } catch {
        Write-Host "Playwright browser installation skipped: $($_.Exception.Message)" -ForegroundColor Yellow
    }
}

# 7. Launch App
Write-Host "Starting Brahma AI..." -ForegroundColor Green
if (Test-Path $VenvPythonW) {
    Start-Process -FilePath $VenvPythonW -ArgumentList "main.py --startup" -WorkingDirectory $WorkingDir
} else {
    Start-Process -FilePath $VenvPython -ArgumentList "main.py --startup" -WorkingDirectory $WorkingDir -WindowStyle Hidden
}

Write-Host "Bootstrap complete. You can close this window."
Start-Sleep -Seconds 3
