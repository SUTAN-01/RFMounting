param(
    [string]$Python = "python"
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

& $Python -m pip install --upgrade pip
& $Python -m pip install -r build_requirements.txt
& $Python -m pip install .
& $Python -m PyInstaller --noconfirm --clean remote_share.spec

$OutDir = Join-Path $Root "dist\windows-package"
New-Item -ItemType Directory -Force -Path $OutDir | Out-Null
Copy-Item -Recurse -Force (Join-Path $Root "dist\remote-share-mount\*") $OutDir
Copy-Item -Force README.md $OutDir

$Launcher = @'
@echo off
start "" "%~dp0remote-share-mount.exe"
'@
Set-Content -Path (Join-Path $OutDir "Launch Remote Share Mount.bat") -Value $Launcher -Encoding ASCII

Write-Host "Windows package prepared at: $OutDir"
