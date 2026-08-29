# Prabha Dairy - Windows Service Installation
# Run as Administrator: .\install-windows-service.ps1

$ErrorActionPreference = "Stop"

Write-Host "Installing Prabha Dairy API as Windows Service" -ForegroundColor Cyan
Write-Host "=" * 70

# Configuration
$ServiceName = "PrabhaDairyAPI"
$DisplayName = "Prabha Dairy API Server"
$Description = "Production API server for Prabha Dairy management system"
$ProjectPath = $PSScriptRoot | Split-Path -Parent
$PythonExe = Join-Path $ProjectPath "venv\Scripts\python.exe"
$MainScript = Join-Path $ProjectPath "api\main.py"

# Check if Python virtual environment exists
if (-not (Test-Path $PythonExe)) {
    Write-Host "Error: Python virtual environment not found at: $PythonExe" -ForegroundColor Red
    Write-Host "Run: python -m venv venv" -ForegroundColor Yellow
    exit 1
}

# Check if .env exists
$EnvFile = Join-Path $ProjectPath ".env"
if (-not (Test-Path $EnvFile)) {
    Write-Host "Error: .env file not found. Copy .env.example to .env and configure it." -ForegroundColor Red
    exit 1
}

# Install NSSM (Non-Sucking Service Manager) if not present
$NssmPath = Join-Path $ProjectPath "deploy\nssm.exe"
if (-not (Test-Path $NssmPath)) {
    Write-Host "Downloading NSSM..." -ForegroundColor Yellow
    $NssmUrl = "https://nssm.cc/release/nssm-2.24.zip"
    $NssmZip = Join-Path $env:TEMP "nssm.zip"
    Invoke-WebRequest -Uri $NssmUrl -OutFile $NssmZip
    Expand-Archive -Path $NssmZip -DestinationPath $env:TEMP -Force
    Copy-Item "$env:TEMP\nssm-2.24\win64\nssm.exe" $NssmPath
    Remove-Item $NssmZip
}

# Stop and remove existing service
$ExistingService = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
if ($ExistingService) {
    Write-Host "Removing existing service..." -ForegroundColor Yellow
    & $NssmPath stop $ServiceName
    & $NssmPath remove $ServiceName confirm
    Start-Sleep -Seconds 2
}

# Install service
Write-Host "Installing Windows Service..." -ForegroundColor Green
& $NssmPath install $ServiceName $PythonExe "-m" "uvicorn" "api.main:app" "--host" "0.0.0.0" "--port" "8000"
& $NssmPath set $ServiceName AppDirectory $ProjectPath
& $NssmPath set $ServiceName DisplayName $DisplayName
& $NssmPath set $ServiceName Description $Description
& $NssmPath set $ServiceName Start SERVICE_AUTO_START

# Configure stdout/stderr logging
$LogDir = Join-Path $ProjectPath "logs"
if (-not (Test-Path $LogDir)) {
    New-Item -ItemType Directory -Path $LogDir | Out-Null
}
& $NssmPath set $ServiceName AppStdout (Join-Path $LogDir "service-stdout.log")
& $NssmPath set $ServiceName AppStderr (Join-Path $LogDir "service-stderr.log")
& $NssmPath set $ServiceName AppRotateFiles 1
& $NssmPath set $ServiceName AppRotateBytes 10485760  # 10MB

Write-Host ""
Write-Host "✅ Service installed successfully!" -ForegroundColor Green
Write-Host ""
Write-Host "To start the service:"
Write-Host "  Start-Service $ServiceName" -ForegroundColor Yellow
Write-Host ""
Write-Host "To check status:"
Write-Host "  Get-Service $ServiceName" -ForegroundColor Yellow
Write-Host ""
Write-Host "To view logs:"
Write-Host "  Get-Content $LogDir\service-stdout.log -Tail 50" -ForegroundColor Yellow
