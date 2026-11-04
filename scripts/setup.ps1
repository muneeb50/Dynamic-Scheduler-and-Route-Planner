# One-time setup (Windows PowerShell). Run from the repo root:  powershell -ExecutionPolicy Bypass -File scripts\setup.ps1
$ErrorActionPreference = "Stop"
Set-Location (Join-Path $PSScriptRoot "..")
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\pip.exe install -r backend\requirements.txt
if ($env:SKIP_YOLO -ne "1") {
  Write-Host "Installing YOLO (Ultralytics + PyTorch CPU) for video analytics - this is the large download..."
  .\.venv\Scripts\pip.exe install -r computer_vision\requirements-yolo.txt
  if ($LASTEXITCODE -ne 0) { Write-Host "YOLO install failed - video analytics will use OpenCV DNN / motion detection instead." }
}
if ($env:WITH_SUMO -eq "1") { .\.venv\Scripts\pip.exe install -r backend\requirements-sumo.txt }
.\.venv\Scripts\python.exe simulation\tools\make_demo_network.py
if (Get-Command npm -ErrorAction SilentlyContinue) {
  Push-Location admin_dashboard; npm install; npm run build; Pop-Location
} else {
  Write-Host "npm not found - skipping dashboard build (a prebuilt copy may already be in admin_dashboard\dist)"
}
Write-Host "Setup complete. Start with: powershell -ExecutionPolicy Bypass -File scripts\run.ps1"
