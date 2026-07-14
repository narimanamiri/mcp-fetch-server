# Start the full Docker stack (MCP server + SearXNG + admin GUI)
# Usage: .\scripts\docker-up.ps1

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
Set-Location $ProjectRoot

if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    Write-Error "Docker is not installed or not on PATH. Install Docker Desktop first."
}

$EnvFile = Join-Path $ProjectRoot ".env"
$EnvExample = Join-Path $ProjectRoot ".env.docker.example"

if (-not (Test-Path $EnvFile)) {
    if (Test-Path $EnvExample) {
        Copy-Item $EnvExample $EnvFile
        Write-Host "Created .env from .env.docker.example — edit MCP_AUTH_TOKEN before exposing this stack." -ForegroundColor Yellow
    } else {
        Write-Error "Missing .env file. Copy .env.docker.example to .env and set MCP_AUTH_TOKEN."
    }
}

$tokenLine = Get-Content $EnvFile | Where-Object { $_ -match '^\s*MCP_AUTH_TOKEN=' } | Select-Object -First 1
if ($tokenLine -match 'change-me') {
    Write-Warning "MCP_AUTH_TOKEN is still the default placeholder — change it in .env before production use."
}

$Workspace = Join-Path $ProjectRoot "workspace"
if (-not (Test-Path $Workspace)) {
    New-Item -ItemType Directory -Path $Workspace | Out-Null
}

Write-Host "Building and starting containers..."
docker compose up -d --build

if ($LASTEXITCODE -ne 0) {
    Write-Error "docker compose up failed"
}

Write-Host ""
Write-Host "Stack is up:" -ForegroundColor Green
Write-Host "  MCP endpoint : http://127.0.0.1:8000/mcp"
Write-Host "  Admin GUI    : http://127.0.0.1:8000/admin"
Write-Host "  Health check : http://127.0.0.1:8000/health"
Write-Host "  SearXNG      : http://127.0.0.1:8080"
Write-Host ""
Write-Host "View logs: docker compose logs -f"
Write-Host "Stop stack: docker compose down"
