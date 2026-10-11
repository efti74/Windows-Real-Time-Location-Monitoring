# Build OrgAssetAgent.exe standalone executable (Optimized Small Footprint)
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $scriptDir

$python = Join-Path $scriptDir ".venv\Scripts\python.exe"

if (-not (Test-Path $python)) {
    Write-Error "Virtual environment not found! Please run setup first."
    exit 1
}

$upxDir = Join-Path $scriptDir "tools\upx"
$upxArgs = @()
if (Test-Path (Join-Path $upxDir "upx.exe")) {
    $upxArgs = @("--upx-dir", $upxDir, "--upx-exclude", "_uuid.pyd")
    Write-Host "UPX binary compression enabled (LZMA high ratio)." -ForegroundColor Green
} else {
    Write-Host "Notice: tools\upx\upx.exe not found. Proceeding without UPX binary compression." -ForegroundColor Yellow
}

Write-Host "Compiling OrgAssetAgent.exe with PyInstaller (Bytecode Opt Level 2, UAC-Admin & Excludes)..." -ForegroundColor Cyan
& $python -m PyInstaller --clean --onefile --name OrgAssetAgent --noconfirm `
    --optimize 2 `
    --uac-admin `
    @upxArgs `
    --exclude-module setuptools `
    --exclude-module pkg_resources `
    --exclude-module distutils `
    --exclude-module pip `
    --exclude-module xml `
    --exclude-module pyexpat `
    --exclude-module multiprocessing `
    --exclude-module _multiprocessing `
    --exclude-module decimal `
    --exclude-module _decimal `
    --exclude-module lzma `
    --exclude-module _lzma `
    --exclude-module bz2 `
    --exclude-module _bz2 `
    --exclude-module unittest `
    --exclude-module test `
    --exclude-module pydoc `
    --exclude-module doctest `
    --exclude-module difflib `
    --exclude-module packaging `
    --exclude-module sqlite3 `
    --exclude-module requests `
    --exclude-module urllib3 `
    --exclude-module certifi `
    --exclude-module charset_normalizer `
    --exclude-module idna `
    --exclude-module dotenv `
    agent/agent.py


if ($LASTEXITCODE -eq 0) {
    $exePath = Join-Path $scriptDir "dist\OrgAssetAgent.exe"
    $sizeMB = [math]::round((Get-Item $exePath).Length / 1MB, 2)
    Write-Host "`n[SUCCESS] Standalone executable created:" -ForegroundColor Green
    Write-Host "  - dist\OrgAssetAgent.exe ($sizeMB MB)" -ForegroundColor Green
} else {
    Write-Error "Build failed!"
}

