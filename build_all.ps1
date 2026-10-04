param(
    [switch]$SkipOmniRoute
)

Write-Host "Building Brahma Evo Application..." -ForegroundColor Cyan

if (-not $SkipOmniRoute) {
    Write-Host "Preparing pinned OmniRoute runtime..." -ForegroundColor Cyan
    & .\.venv\Scripts\python.exe scripts\prepare_omniroute_runtime.py build_vendor\omniroute_runtime
    if ($LASTEXITCODE -ne 0) {
        Write-Host "Failed to prepare OmniRoute runtime!" -ForegroundColor Red
        exit 1
    }
}

.\.venv\Scripts\pyinstaller.exe installer\BrahmaEvo.spec --noconfirm
if ($LASTEXITCODE -ne 0) {
    Write-Host "Failed to build main application!" -ForegroundColor Red
    exit 1
}

Write-Host "Building headless crash supervisor..." -ForegroundColor Cyan
.\.venv\Scripts\pyinstaller.exe installer\BrahmaEvo_Supervisor.spec --noconfirm
if ($LASTEXITCODE -ne 0) {
    Write-Host "Failed to build crash supervisor!" -ForegroundColor Red
    exit 1
}
if (-not (Test-Path "dist\BrahmaEvoSupervisor.exe")) {
    Write-Host "Crash supervisor executable was not produced!" -ForegroundColor Red
    exit 1
}

Copy-Item "dist\BrahmaEvoSupervisor.exe" "dist\BrahmaEvo\BrahmaEvoSupervisor.exe" -Force
if ($LASTEXITCODE -ne 0) {
    Write-Host "Failed to copy crash supervisor beside the main application!" -ForegroundColor Red
    exit 1
}

Write-Host "Main Application built successfully. Now building Setup Wizard..." -ForegroundColor Cyan
.\.venv\Scripts\pyinstaller.exe installer\BrahmaEvo_Setup.spec --noconfirm
if ($LASTEXITCODE -ne 0) {
    Write-Host "Failed to build setup wizard!" -ForegroundColor Red
    exit 1
}

Write-Host "Build complete! Setup is located in dist\BrahmaEvo_Setup.exe" -ForegroundColor Green
