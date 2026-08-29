# Quick Setup Script for Windows
# Run this as Administrator for easy client deployment

$ErrorActionPreference = "Stop"

Write-Host "================================================" -ForegroundColor Cyan
Write-Host "   Prabha Dairy Dashboard - Quick Setup        " -ForegroundColor Cyan
Write-Host "================================================" -ForegroundColor Cyan
Write-Host ""

# Get the project root directory
$ProjectRoot = Split-Path -Parent $PSScriptRoot

# Step 1: Check Python
Write-Host "[1/7] Checking Python installation..." -ForegroundColor Yellow
try {
    $pythonVersion = python --version 2>&1
    Write-Host "  ✓ Found: $pythonVersion" -ForegroundColor Green
} catch {
    Write-Host "  ✗ Python not found!" -ForegroundColor Red
    Write-Host ""
    Write-Host "Please install Python 3.11 from:" -ForegroundColor Yellow
    Write-Host "https://www.python.org/downloads/" -ForegroundColor Cyan
    Write-Host ""
    Write-Host "Make sure to check 'Add Python to PATH' during installation" -ForegroundColor Yellow
    exit 1
}

# Step 2: Check PostgreSQL
Write-Host "[2/7] Checking PostgreSQL installation..." -ForegroundColor Yellow
$pgService = Get-Service -Name "postgresql*" -ErrorAction SilentlyContinue
if ($pgService) {
    Write-Host "  ✓ PostgreSQL service found: $($pgService.Name)" -ForegroundColor Green
} else {
    Write-Host "  ⚠ PostgreSQL not detected" -ForegroundColor Yellow
    Write-Host "  Please install PostgreSQL 14 from:" -ForegroundColor Yellow
    Write-Host "  https://www.postgresql.org/download/windows/" -ForegroundColor Cyan
    $continue = Read-Host "  Continue anyway? (y/n)"
    if ($continue -ne "y") { exit 1 }
}

# Step 3: Create virtual environment
Write-Host "[3/7] Setting up Python virtual environment..." -ForegroundColor Yellow
$venvPath = Join-Path $ProjectRoot "venv"
if (-not (Test-Path $venvPath)) {
    python -m venv $venvPath
    Write-Host "  ✓ Virtual environment created" -ForegroundColor Green
} else {
    Write-Host "  ✓ Virtual environment already exists" -ForegroundColor Green
}

# Step 4: Install dependencies
Write-Host "[4/7] Installing dependencies (this may take 2-3 minutes)..." -ForegroundColor Yellow
$pipExe = Join-Path $venvPath "Scripts\pip.exe"
& $pipExe install --upgrade pip --quiet
& $pipExe install -r (Join-Path $ProjectRoot "requirements.txt") --quiet
Write-Host "  ✓ All dependencies installed" -ForegroundColor Green

# Step 5: Configure environment
Write-Host "[5/7] Configuring environment..." -ForegroundColor Yellow
$envFile = Join-Path $ProjectRoot ".env"
$envExample = Join-Path $ProjectRoot ".env.example"

if (-not (Test-Path $envFile)) {
    if (Test-Path $envExample) {
        Copy-Item $envExample $envFile
        Write-Host "  ✓ Created .env file from template" -ForegroundColor Green
        Write-Host ""
        Write-Host "  IMPORTANT: Edit .env file with your settings:" -ForegroundColor Yellow
        Write-Host "    1. DATABASE_URL - PostgreSQL connection" -ForegroundColor White
        Write-Host "    2. TALLY_HOST - Your Tally server IP" -ForegroundColor White
        Write-Host "    3. TALLY_PORT - Usually 9000" -ForegroundColor White
        Write-Host "    4. SECRET_KEY - Generate with: openssl rand -hex 32" -ForegroundColor White
        Write-Host ""

        # Ask for Tally details
        Write-Host "  Let's configure Tally connection now:" -ForegroundColor Cyan
        $tallyHost = Read-Host "  Enter Tally server IP (or localhost)"
        $tallyPort = Read-Host "  Enter Tally port (default 9000)"
        if (-not $tallyPort) { $tallyPort = "9000" }

        # Update .env file
        $envContent = Get-Content $envFile
        $envContent = $envContent -replace "TALLY_HOST=.*", "TALLY_HOST=$tallyHost"
        $envContent = $envContent -replace "TALLY_PORT=.*", "TALLY_PORT=$tallyPort"
        $envContent | Set-Content $envFile

        Write-Host "  ✓ Tally connection configured" -ForegroundColor Green
    } else {
        Write-Host "  ✗ .env.example not found!" -ForegroundColor Red
        exit 1
    }
} else {
    Write-Host "  ✓ .env file already exists" -ForegroundColor Green
}

# Step 6: Test Tally connection
Write-Host "[6/7] Testing Tally ERP connection..." -ForegroundColor Yellow
$pythonExe = Join-Path $venvPath "Scripts\python.exe"
$testScript = @"
import os
from dotenv import load_dotenv
load_dotenv()
from tally_connector import test_connection
try:
    result = test_connection()
    print(f"✓ Connected to Tally: {result}")
    exit(0)
except Exception as e:
    print(f"✗ Connection failed: {e}")
    exit(1)
"@

$testFile = Join-Path $env:TEMP "test_tally.py"
$testScript | Out-File -FilePath $testFile -Encoding UTF8

try {
    & $pythonExe $testFile
    Write-Host "  ✓ Tally connection successful" -ForegroundColor Green
} catch {
    Write-Host "  ⚠ Could not connect to Tally (will retry later)" -ForegroundColor Yellow
    Write-Host "  Make sure Tally is running and port 9000 is open" -ForegroundColor Yellow
}

Remove-Item $testFile -ErrorAction SilentlyContinue

# Step 7: Initialize database
Write-Host "[7/7] Initializing database..." -ForegroundColor Yellow
try {
    Set-Location $ProjectRoot
    & $pythonExe -m database.migrate
    Write-Host "  ✓ Database schema created" -ForegroundColor Green
} catch {
    Write-Host "  ⚠ Database initialization failed" -ForegroundColor Yellow
    Write-Host "  Please configure DATABASE_URL in .env file" -ForegroundColor Yellow
}

# Final instructions
Write-Host ""
Write-Host "================================================" -ForegroundColor Green
Write-Host "   Setup Complete! 🎉" -ForegroundColor Green
Write-Host "================================================" -ForegroundColor Green
Write-Host ""
Write-Host "Next steps:" -ForegroundColor Cyan
Write-Host ""
Write-Host "1. Start the development server:" -ForegroundColor White
Write-Host "   .\venv\Scripts\activate" -ForegroundColor Yellow
Write-Host "   uvicorn api.main:app --reload --port 8000" -ForegroundColor Yellow
Write-Host ""
Write-Host "2. Open in browser:" -ForegroundColor White
Write-Host "   http://localhost:8000" -ForegroundColor Cyan
Write-Host ""
Write-Host "3. To install as Windows Service (run as Administrator):" -ForegroundColor White
Write-Host "   .\deploy\install-windows-service.ps1" -ForegroundColor Yellow
Write-Host ""
Write-Host "For production deployment, see: DEPLOYMENT.md" -ForegroundColor White
Write-Host ""

# Ask if they want to start the server now
$startNow = Read-Host "Start the server now? (y/n)"
if ($startNow -eq "y") {
    Write-Host ""
    Write-Host "Starting server..." -ForegroundColor Cyan
    Write-Host "Press Ctrl+C to stop" -ForegroundColor Yellow
    Write-Host ""
    Start-Sleep -Seconds 2

    # Activate venv and start server
    $activateScript = Join-Path $venvPath "Scripts\Activate.ps1"
    & $activateScript
    & uvicorn api.main:app --reload --port 8000
}
