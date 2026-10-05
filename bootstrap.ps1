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
$NodeNeedsRepair = $true
if (Get-Command "node" -ErrorAction SilentlyContinue) {
    try {
        $NodeDetected = (& node --version).Trim().TrimStart("v")
        if ($NodeDetected -eq $NodeVersion) {
            $NodeNeedsRepair = $false
            Write-Host "Node.js is already installed at the canonical version v$NodeVersion." -ForegroundColor Green
        } else {
            Write-Host "Node.js version $NodeDetected is not the canonical v$NodeVersion; repairing it..." -ForegroundColor Yellow
        }
    } catch {
        Write-Host "Node.js version could not be verified; repairing it..." -ForegroundColor Yellow
    }
}
if ($NodeNeedsRepair) {
    Write-Host "Downloading Node v$NodeVersion..." -ForegroundColor Yellow
    $NodeUrl = "https://nodejs.org/dist/v$NodeVersion/node-v$NodeVersion-x64.msi"
    $NodeInstaller = "$env:TEMP\node-v$NodeVersion-installer.msi"
    Invoke-WebRequest -Uri $NodeUrl -OutFile $NodeInstaller
    
    Write-Host "Installing Node.js v$NodeVersion (Silent Mode)..." -ForegroundColor Yellow
    Start-Process -FilePath "msiexec.exe" -ArgumentList @("/i", $NodeInstaller, "/qn") -Wait
    
    Update-Environment
    try {
        $NodeDetected = (& node --version).Trim().TrimStart("v")
    } catch {
        $NodeDetected = ""
    }
    if ($NodeDetected -ne $NodeVersion) {
        throw "Installed Node.js version '$NodeDetected' does not match canonical version '$NodeVersion'."
    }
}

# 5. Virtual Environment Setup
$VenvDir = Join-Path -Path $WorkingDir -ChildPath ".venv"
$VenvPython = Join-Path -Path $VenvDir -ChildPath "Scripts\python.exe"
$VenvPythonW = Join-Path -Path $VenvDir -ChildPath "Scripts\pythonw.exe"

$VenvNeedsRecreate = -not (Test-Path $VenvPython)
if (-not $VenvNeedsRecreate) {
    try {
        $VenvVersionCheck = & $VenvPython -c "import sys; expected=tuple(int(x) for x in '$PythonMajorMinor'.split('.')); raise SystemExit(0 if sys.version_info[:2] == expected else 1)"
        if ($LASTEXITCODE -ne 0) {
            $VenvNeedsRecreate = $true
            Write-Host "Existing .venv uses a different Python major/minor; recreating it." -ForegroundColor Yellow
        }
    } catch {
        $VenvNeedsRecreate = $true
        Write-Host "Existing .venv could not be validated; recreating it." -ForegroundColor Yellow
    }
}
if ($VenvNeedsRecreate) {
    Write-Host "Creating Virtual Environment in .venv..." -ForegroundColor Cyan
    if (Test-Path $VenvDir) { Remove-Item -Recurse -Force $VenvDir }
    $venvArgs = @()
    $venvArgs += $PythonArgs
    $venvArgs += @("-m", "venv", ".venv")
    Start-Process -FilePath $PythonExe -ArgumentList $venvArgs -Wait -NoNewWindow
    if (-not (Test-Path $VenvPython)) {
        throw "Virtual environment creation failed: $VenvPython was not created."
    }
    Write-Host "Virtual Environment created." -ForegroundColor Green
} else {
    Write-Host "Virtual Environment already exists and matches Python $PythonMajorMinor." -ForegroundColor Green
}

# 6. Verify/repair dependencies against the actual application import graph.
$NeedsRepair = $false
try {
    $env:BRAHMA_EVO_TEST_MODE = "1"
    $env:BRAHMA_SKIP_STARTUP_UPDATE = "1"
    & $VenvPython -c "import main" | Out-Null
    if ($LASTEXITCODE -ne 0) { $NeedsRepair = $true }
} catch {
    $NeedsRepair = $true
} finally {
    Remove-Item Env:BRAHMA_EVO_TEST_MODE -ErrorAction SilentlyContinue
    Remove-Item Env:BRAHMA_SKIP_STARTUP_UPDATE -ErrorAction SilentlyContinue
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

# 7. Launch the independent supervisor; it owns the Brahma process lifecycle.
Write-Host "Starting Brahma Evo supervisor..." -ForegroundColor Green
$SupervisorExe = Join-Path $WorkingDir "BrahmaEvoSupervisor.exe"
$SupervisorPy = Join-Path $WorkingDir "core\process_supervisor.py"

if (Test-Path $SupervisorExe) {
    Start-Process -FilePath $SupervisorExe -WorkingDirectory $WorkingDir -WindowStyle Hidden
} elseif (Test-Path $VenvPythonW) {
    Start-Process -FilePath $VenvPythonW -ArgumentList $SupervisorPy -WorkingDirectory $WorkingDir -WindowStyle Hidden
} else {
    Start-Process -FilePath $VenvPython -ArgumentList $SupervisorPy -WorkingDirectory $WorkingDir -WindowStyle Hidden
}

Write-Host "Bootstrap complete. The supervisor will restart Brahma after unexpected crashes."
Start-Sleep -Seconds 3
