<#
.SYNOPSIS
    Builds the Prabha Dairy Electron installer (.exe) for Windows.

.DESCRIPTION
    1. Creates a portable Python virtual environment inside electron/python-backend/
    2. Copies all Python source, static files, templates, and config
    3. Installs pip dependencies into the venv
    4. Runs electron-builder to produce a single NSIS .exe installer

.NOTES
    Prerequisites:
    - Python 3.11+ on PATH
    - Node.js 18+ on PATH
    - npm on PATH
    - Internet access (first run downloads Electron binaries)

.EXAMPLE
    .\build-installer.ps1
#>

$ErrorActionPreference = "Stop"

$ProjectRoot = Split-Path -Parent $PSScriptRoot
if (-not $ProjectRoot) { $ProjectRoot = (Get-Location).Path }
# If script is at project root already
if (-not (Test-Path (Join-Path $ProjectRoot "api"))) {
    $ProjectRoot = (Get-Location).Path
}

$ElectronDir = Join-Path $ProjectRoot "electron"
$BackendDest = Join-Path $ElectronDir "python-backend"

Write-Host "========================================" -ForegroundColor Cyan
Write-Host " Prabha Dairy - Build Installer" -ForegroundColor Cyan
Write-Host "========================================" -ForegroundColor Cyan
Write-Host ""
Write-Host "Project root : $ProjectRoot"
Write-Host "Electron dir : $ElectronDir"
Write-Host "Backend dest : $BackendDest"
Write-Host ""

# Step 1: Clean previous build
Write-Host "[1/5] Cleaning previous python-backend bundle..." -ForegroundColor Yellow
if (Test-Path $BackendDest) {
    Remove-Item $BackendDest -Recurse -Force
}
New-Item -ItemType Directory -Path $BackendDest -Force | Out-Null

# Step 2: Copy Python source files
Write-Host "[2/5] Copying Python source files..." -ForegroundColor Yellow

# Package/data directories to copy.
# NOTE: If you add a new top-level package folder to the project, add its
# name here too, or the bundled app will fail with ModuleNotFoundError.
$copyDirs = @(
    "api", "database", "static", "templates", "deploy",
    "services", "storage", "tally_extractor", "setup"
)
foreach ($dir in $copyDirs) {
    $src = Join-Path $ProjectRoot $dir
    if (Test-Path $src) {
        $dst = Join-Path $BackendDest $dir
        Copy-Item $src $dst -Recurse -Force
        Write-Host "  Copied $dir/"
    }
    else {
        Write-Host "  Skipped $dir/ (not found)" -ForegroundColor DarkGray
    }
}

# Copy EVERY top-level .py module automatically instead of hand-maintaining
# a list. This is what previously caused repeated ModuleNotFoundError
# failures (analytics_service.py, etc. were missing from a hardcoded list).
Get-ChildItem -Path $ProjectRoot -Filter "*.py" -File | ForEach-Object {
    Copy-Item $_.FullName (Join-Path $BackendDest $_.Name) -Force
    Write-Host "  Copied $($_.Name)"
}

# Non-.py root files still need to be listed explicitly
$rootExtraFiles = @(
    "requirements.txt",
    ".env.example"
)
foreach ($file in $rootExtraFiles) {
    $src = Join-Path $ProjectRoot $file
    if (Test-Path $src) {
        Copy-Item $src (Join-Path $BackendDest $file) -Force
        Write-Host "  Copied $file"
    }
}

# Copy .env if it exists (sanitize before bundling)
$envFile = Join-Path $ProjectRoot ".env"

if (Test-Path $envFile) {
    # Keep only lines that look like KEY=VALUE - everything else is dropped
    $sanitized = Select-String -Path $envFile -Pattern '^[A-Z_][A-Z0-9_]*=' |
                 ForEach-Object { $_.Line }

    if ($sanitized.Count -gt 0) {
        $sanitized | Set-Content (Join-Path $BackendDest ".env") -Force
        Write-Host "  Sanitized and copied .env"
    }
    else {
        Write-Host "  .env file exists but contains no valid entries - skipping copy"
    }
}

# Step 3: Create venv and install dependencies
Write-Host "[3/5] Creating Python virtual environment..." -ForegroundColor Yellow
$venvPath = Join-Path $BackendDest "venv"

python -m venv $venvPath
if ($LASTEXITCODE -ne 0) {
    Write-Host "ERROR: Failed to create Python venv. Is Python 3.11+ on PATH?" -ForegroundColor Red
    exit 1
}

$pipExe = Join-Path $venvPath "Scripts\pip.exe"
$reqFile = Join-Path $BackendDest "requirements.txt"

Write-Host "[3/5] Installing Python dependencies (this may take a few minutes)..." -ForegroundColor Yellow
& $pipExe install --no-cache-dir -r $reqFile
if ($LASTEXITCODE -ne 0) {
    Write-Host "ERROR: pip install failed." -ForegroundColor Red
    exit 1
}
Write-Host "  Dependencies installed successfully." -ForegroundColor Green

# Step 4: Install Electron dependencies
Write-Host "[4/5] Installing Electron dependencies..." -ForegroundColor Yellow
Push-Location $ElectronDir
npm install
if ($LASTEXITCODE -ne 0) {
    Write-Host "ERROR: npm install failed." -ForegroundColor Red
    Pop-Location
    exit 1
}
Pop-Location

# Step 5: Build installer
Write-Host "[5/5] Building Windows installer with electron-builder..." -ForegroundColor Yellow
Push-Location $ElectronDir
npx electron-builder --win
if ($LASTEXITCODE -ne 0) {
    Write-Host "ERROR: electron-builder failed." -ForegroundColor Red
    Pop-Location
    exit 1
}
Pop-Location

# Done
$outputDir = Join-Path $ElectronDir "build"
Write-Host ""
Write-Host "========================================" -ForegroundColor Green
Write-Host " BUILD COMPLETE!" -ForegroundColor Green
Write-Host "========================================" -ForegroundColor Green
Write-Host ""
Write-Host "Installer location:" -ForegroundColor Cyan
Get-ChildItem (Join-Path $outputDir "*.exe") -ErrorAction SilentlyContinue | ForEach-Object {
    Write-Host "  $($_.FullName)" -ForegroundColor White
}
Write-Host ""
Write-Host "To distribute: copy the .exe to the target machine and run it."
Write-Host "The user should place their .env file next to the installed .exe."
Write-Host ""
