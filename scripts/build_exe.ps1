# Build mcp-fetch-server Windows executable with PyInstaller
# Usage: .\scripts\build_exe.ps1

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
Set-Location $ProjectRoot

Write-Host "Installing build dependencies..."
uv add --dev pyinstaller

Write-Host "Building executable..."
uv run pyinstaller --noconfirm --clean mcp_fetch_server.spec

$ExePath = Join-Path $ProjectRoot "dist\mcp-fetch-server.exe"
if (Test-Path $ExePath) {
    $size = (Get-Item $ExePath).Length / 1MB
    Write-Host ""
    Write-Host "Build successful!" -ForegroundColor Green
    Write-Host "Executable: $ExePath"
    Write-Host ("Size: {0:N1} MB" -f $size)
    Write-Host ""
    Write-Host "Test with:"
    Write-Host "  .\dist\mcp-fetch-server.exe --help"
    Write-Host "  .\dist\mcp-fetch-server.exe --transport stdio"
} else {
    Write-Error "Build failed: executable not found at $ExePath"
}
