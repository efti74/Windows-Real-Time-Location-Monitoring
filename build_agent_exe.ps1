# Build OrgAssetAgent.exe standalone executable - Defender-friendly, no UPX packing
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $scriptDir

$python = Join-Path $scriptDir ".venv\Scripts\python.exe"

if (-not (Test-Path $python)) {
    Write-Error "Virtual environment not found! Please run setup first."
    exit 1
}

# ---------------------------------------------------------------
# Version info file - Defender and Windows trust apps with version
# metadata embedded in the PE header (ProductName, CompanyName etc.)
# ---------------------------------------------------------------
$versionInfoPath = Join-Path $scriptDir "agent\version_info.txt"
Set-Content -Path $versionInfoPath -Encoding utf8 -Value @"
VSVersionInfo(
  ffi=FixedFileInfo(
    filevers=(1, 0, 0, 0),
    prodvers=(1, 0, 0, 0),
    mask=0x3f,
    flags=0x0,
    OS=0x40004,
    fileType=0x1,
    subtype=0x0,
    date=(0, 0)
  ),
  kids=[
    StringFileInfo([
      StringTable(
        u'040904B0',
        [StringStruct(u'CompanyName', u'Windows Real-Time Location Monitoring'),
         StringStruct(u'FileDescription', u'Windows Real-Time Location Monitoring Agent'),
         StringStruct(u'FileVersion', u'1.0.0.0'),
         StringStruct(u'InternalName', u'OrgAssetAgent'),
         StringStruct(u'LegalCopyright', u'Copyright 2026'),
         StringStruct(u'OriginalFilename', u'OrgAssetAgent.exe'),
         StringStruct(u'ProductName', u'Windows Real-Time Location Monitoring'),
         StringStruct(u'ProductVersion', u'1.0.0.0')])
    ]),
    VarFileInfo([VarStruct(u'Translation', [1033, 1200])])
  ]
)
"@

$manifestPath = Join-Path $scriptDir "agent\OrgAssetAgent.manifest"

Write-Host "Building OrgAssetAgent.exe (Defender-friendly: no UPX, manifest + version embedded)..." -ForegroundColor Cyan

$pyArgs = @(
    "-m", "PyInstaller",
    "--clean", "--onefile",
    "--name", "OrgAssetAgent",
    "--noconfirm",
    "--optimize", "2",
    "--noupx",
    "--uac-admin",
    "--manifest", $manifestPath,
    "--version-file", $versionInfoPath,
    "--exclude-module", "setuptools",
    "--exclude-module", "pkg_resources",
    "--exclude-module", "distutils",
    "--exclude-module", "pip",
    "--exclude-module", "xml",
    "--exclude-module", "pyexpat",
    "--exclude-module", "multiprocessing",
    "--exclude-module", "_multiprocessing",
    "--exclude-module", "decimal",
    "--exclude-module", "_decimal",
    "--exclude-module", "lzma",
    "--exclude-module", "_lzma",
    "--exclude-module", "bz2",
    "--exclude-module", "_bz2",
    "--exclude-module", "unittest",
    "--exclude-module", "test",
    "--exclude-module", "pydoc",
    "--exclude-module", "doctest",
    "--exclude-module", "difflib",
    "--exclude-module", "packaging",
    "--exclude-module", "sqlite3",
    "--exclude-module", "requests",
    "--exclude-module", "urllib3",
    "--exclude-module", "certifi",
    "--exclude-module", "charset_normalizer",
    "--exclude-module", "idna",
    "--exclude-module", "winsdk",
    "--exclude-module", "winrt",
    "--exclude-module", "dotenv",
    "agent/agent.py"
)

& $python @pyArgs

if ($LASTEXITCODE -eq 0) {
    $exePath = Join-Path $scriptDir "dist\OrgAssetAgent.exe"
    $sizeMB = [math]::round((Get-Item $exePath).Length / 1MB, 2)
    Write-Host "`n[SUCCESS] Defender-friendly executable created:" -ForegroundColor Green
    Write-Host "  - dist\OrgAssetAgent.exe ($sizeMB MB) -- No UPX packing, manifest + version embedded" -ForegroundColor Green
} else {
    Write-Error "Build failed!"
}
