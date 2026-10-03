<#
.SYNOPSIS
    One-command setup for the MCP fetch server and its offline corpus stack.

.DESCRIPTION
    Takes a bare machine to a working install: fetches the project if it is not
    already here, installs dependencies, writes a configuration file with a
    freshly generated auth token, checks the local model, brings up the vector
    store, and verifies the result.

    Run it from inside a checkout:

        .\scripts\install.ps1

    ...or standalone on a machine with nothing but git, in which case it clones
    the project first:

        irm https://raw.githubusercontent.com/narimanamiri/mcp-fetch-server/master/scripts/install.ps1 -OutFile install.ps1
        powershell -ExecutionPolicy Bypass -File install.ps1 -Yes

    Re-running is safe. Nothing already configured is overwritten: an existing
    .env is left alone, models already pulled are skipped, and a running vector
    store is reused.

.EXAMPLE
    .\scripts\install.ps1 -Yes -Mode offline -Qdrant embedded

.EXAMPLE
    .\scripts\install.ps1 -Yes -Corpus C:\documents -Watch
#>

[CmdletBinding()]
param(
    # Where to install. Defaults to this checkout, else .\mcp-fetch-server
    [string] $Dir = "",

    # Git branch or tag to clone.
    [string] $Ref = "master",

    # Corpus mode.
    [ValidateSet("online", "offline", "hybrid")]
    [string] $Mode = "hybrid",

    # Vector store: server runs Qdrant in Docker, embedded uses local disk.
    [ValidateSet("auto", "server", "embedded")]
    [string] $Qdrant = "auto",

    # Folder to ingest once setup finishes.
    [string] $Corpus = "",

    # Also install the cross-encoder reranker (large: onnxruntime).
    [switch] $Rerank,

    # Do not pull Ollama models (~4.5 GB).
    [switch] $NoModels,

    # Register the folder watcher as a scheduled task.
    [switch] $Watch,

    # Assume yes to prompts. Required for unattended runs.
    [switch] $Yes,

    # Fail instead of installing uv when it is missing.
    [switch] $NoUvInstall
)

$ErrorActionPreference = "Stop"

$RepoUrl      = if ($env:MCP_REPO_URL) { $env:MCP_REPO_URL } else { "https://github.com/narimanamiri/mcp-fetch-server.git" }
$UvInstaller  = "https://astral.sh/uv/install.ps1"
$ModelEmbed   = "bge-m3"
$ModelChat    = "gemma3:4b"
$TaskName     = "MCP corpus watcher"

# ------------------------------------------------------------------ output

function Write-Step { param([string] $Text) Write-Host "`n==> $Text" -ForegroundColor Green }
function Write-Info { param([string] $Text) Write-Host "    $Text" }
function Write-Warn { param([string] $Text) Write-Host "    ! $Text" -ForegroundColor Yellow }
function Stop-WithError {
    param([string] $Text)
    Write-Host "`nerror: $Text" -ForegroundColor Red
    exit 1
}
function Test-Have { param([string] $Name) return [bool](Get-Command $Name -ErrorAction SilentlyContinue) }

function Confirm-Action {
    param([string] $Prompt)
    if ($Yes) { return $true }
    # A non-interactive host must be explicit rather than assumed.
    if ([Console]::IsInputRedirected) {
        Write-Warn "not interactive and -Yes was not given, so skipping: $Prompt"
        return $false
    }
    $reply = Read-Host "    $Prompt [y/N]"
    return $reply -match '^[yY]'
}

function Test-UrlAlive {
    param([string] $Url, [int] $TimeoutSec = 3)
    try {
        Invoke-WebRequest -Uri $Url -TimeoutSec $TimeoutSec -UseBasicParsing | Out-Null
        return $true
    } catch { return $false }
}

Write-Host "MCP fetch server installer"
Write-Info "platform: windows"

# ------------------------------------------------------- 1. project

Write-Step "Locating the project"

$scriptDir = if ($PSScriptRoot) { $PSScriptRoot } else { (Get-Location).Path }
$projectDir = $null

if ($Dir) {
    $projectDir = $Dir
} elseif (Test-Path (Join-Path $scriptDir "..\pyproject.toml")) {
    $projectDir = (Resolve-Path (Join-Path $scriptDir "..")).Path
} elseif ((Test-Path ".\pyproject.toml") -and
         (Select-String -Path ".\pyproject.toml" -Pattern 'name = "mcp-fetch-server"' -Quiet)) {
    $projectDir = (Get-Location).Path
} else {
    $projectDir = Join-Path (Get-Location).Path "mcp-fetch-server"
}

if (Test-Path (Join-Path $projectDir "pyproject.toml")) {
    Write-Info "using existing checkout: $projectDir"
} else {
    if (-not (Test-Have git)) { Stop-WithError "git is required to fetch the project. Install git and re-run." }
    Write-Info "cloning $RepoUrl ($Ref)"
    Write-Info "  into $projectDir"
    & git clone --depth 1 --branch $Ref $RepoUrl $projectDir
    if ($LASTEXITCODE -ne 0) { Stop-WithError "clone failed. Check the URL, the ref, and your network." }
}

Set-Location $projectDir
$projectDir = (Get-Location).Path

# Check the checkout is complete now rather than tripping over a missing file
# several steps later, where the error points at the wrong thing. An
# interrupted clone, a full disk, or a partial copy all land here.
foreach ($required in @("pyproject.toml", ".env.example", "src\mcp_fetch_server\__init__.py")) {
    if (-not (Test-Path $required)) {
        Stop-WithError @"
the checkout at $projectDir is incomplete: $required is missing.
Delete that directory and re-run, or clone it again:
    git clone $RepoUrl
"@
    }
}

# Windows truncates at MAX_PATH (260) unless long paths are enabled, and some
# dependencies ship deeply nested data files. jsonschema_specifications alone
# needs about 100 characters past the project root, so a deep install folder
# produces a venv that imports and then fails with FileNotFoundError on a file
# that is plainly there -- confusing enough to be worth catching up front.
$longPaths = 0
try {
    $longPaths = (Get-ItemProperty "HKLM:\SYSTEM\CurrentControlSet\Control\FileSystem" `
        -Name LongPathsEnabled -ErrorAction Stop).LongPathsEnabled
} catch { }

if ($longPaths -ne 1 -and $projectDir.Length -gt 130) {
    Write-Warn "the install path is $($projectDir.Length) characters, which is long for Windows."
    Write-Info "Some packages nest data files ~100 characters deeper, so they may fail to"
    Write-Info "load even though they installed. Either install somewhere shorter, such as"
    Write-Info "C:\mcp-fetch-server, or enable long paths (as administrator):"
    Write-Info '  New-ItemProperty -Path "HKLM:\SYSTEM\CurrentControlSet\Control\FileSystem" `'
    Write-Info '    -Name LongPathsEnabled -Value 1 -PropertyType DWORD -Force'
    if (-not (Confirm-Action "Continue anyway?")) {
        Stop-WithError "stopped. Re-run with --dir pointing somewhere shorter."
    }
}

# ------------------------------------------------------- 2. uv

Write-Step "Checking for uv"

if (Test-Have uv) {
    Write-Info "found: $(uv --version)"
} else {
    if ($NoUvInstall) {
        Stop-WithError "uv is not installed and -NoUvInstall was given.`nInstall it yourself:  irm $UvInstaller | iex"
    }
    # Installing uv means downloading and running a remote script, so it is not
    # done silently.
    Write-Warn "uv is not installed."
    Write-Info "The official installer at $UvInstaller would be downloaded and run."
    if (Confirm-Action "Install uv now?") {
        Invoke-RestMethod $UvInstaller | Invoke-Expression
        # The installer does not refresh this process's PATH.
        $uvBin = Join-Path $env:USERPROFILE ".local\bin"
        if (Test-Path (Join-Path $uvBin "uv.exe")) { $env:PATH = "$uvBin;$env:PATH" }
        if (-not (Test-Have uv)) { Stop-WithError "uv was installed but is not on PATH. Open a new shell and re-run." }
        Write-Info "installed: $(uv --version)"
    } else {
        Stop-WithError "uv is required. Install it and re-run:  irm $UvInstaller | iex"
    }
}

# ------------------------------------------------------- 3. deps

Write-Step "Installing dependencies"

$extras = @("--extra", "rag")
if ($Rerank) { $extras += @("--extra", "rerank") }
Write-Info ("extras: rag" + $(if ($Rerank) { ", rerank" } else { "" }))

# An installer gets run on whatever network the machine happens to have, and
# uv's 30s default is not enough for the larger wheels (numpy, onnxruntime) on
# a slow link. Retrying is worthwhile because completed downloads are cached,
# so each attempt starts further along.
if (-not $env:UV_HTTP_TIMEOUT) { $env:UV_HTTP_TIMEOUT = "180" }

$syncOk = $false
foreach ($attempt in 1..3) {
    & uv sync @extras
    if ($LASTEXITCODE -eq 0) { $syncOk = $true; break }
    if ($attempt -lt 3) {
        Write-Warn "dependency download failed (attempt $attempt/3), retrying..."
        Start-Sleep -Seconds 5
    }
}
if (-not $syncOk) {
    Stop-WithError @"
dependency installation failed after 3 attempts.
If the network is slow, raise the timeout and try again:
    `$env:UV_HTTP_TIMEOUT = 600; uv sync --extra rag
"@
}

function Invoke-Project {
    # Run a command inside the project environment.
    & uv run --no-sync @args
}

# ------------------------------------------------------- 4. config

Write-Step "Writing configuration"

function Set-EnvValue {
    param([string] $Key, [string] $Value)
    $lines = if (Test-Path ".env") { Get-Content ".env" } else { @() }
    if ($lines -match "^$([regex]::Escape($Key))=") {
        $lines = $lines | ForEach-Object {
            if ($_ -match "^$([regex]::Escape($Key))=") { "$Key=$Value" } else { $_ }
        }
    } else {
        $lines += "$Key=$Value"
    }
    Set-Content ".env" -Value $lines -Encoding utf8
}

if (Test-Path ".env") {
    Write-Info ".env already exists, leaving it untouched"
} else {
    if (-not (Test-Path ".env.example")) { Stop-WithError ".env.example is missing from the checkout." }
    Copy-Item ".env.example" ".env"

    # A real random token: the example ships an obvious placeholder, and an
    # HTTP deployment that keeps it is unauthenticated in practice.
    $bytes = New-Object byte[] 32
    [System.Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
    $token = -join ($bytes | ForEach-Object { $_.ToString("x2") })
    Set-EnvValue "MCP_AUTH_TOKEN" $token
    Write-Info "generated a random MCP_AUTH_TOKEN"
    Write-Info "wrote .env"
}

Set-EnvValue "FETCH_NET_MODE" $Mode
Write-Info "corpus mode: $Mode"

# ------------------------------------------------------- 5. model

Write-Step "Checking the local model"

$ollamaUrl = "http://localhost:11434"
try {
    $probe = Invoke-Project python -c "from mcp_fetch_server.config import settings;print(settings.llm_base_url)"
    if ($probe) { $ollamaUrl = ($probe | Select-Object -Last 1).Trim() }
} catch { }

if (Test-UrlAlive "$ollamaUrl/api/version") {
    Write-Info "Ollama is reachable at $ollamaUrl"
} elseif (Test-Have ollama) {
    Write-Warn "Ollama is installed but not responding; starting it in the background"
    Start-Process -FilePath "ollama" -ArgumentList "serve" -WindowStyle Hidden | Out-Null
    foreach ($i in 1..10) {
        if (Test-UrlAlive "$ollamaUrl/api/version") { break }
        Start-Sleep -Seconds 2
    }
    if (Test-UrlAlive "$ollamaUrl/api/version") { Write-Info "Ollama is now reachable" }
    else { Write-Warn "Ollama still not responding" }
} else {
    Write-Warn "Ollama was not found. The corpus needs it for embeddings and enrichment."
    Write-Info "Install it from https://ollama.com/download, then re-run this script."
}

if ((Test-UrlAlive "$ollamaUrl/api/version") -and -not $NoModels) {
    $installed = @()
    try {
        $tags = Invoke-RestMethod -Uri "$ollamaUrl/api/tags" -TimeoutSec 10
        $installed = @($tags.models | ForEach-Object { ($_.model -split ":")[0] })
    } catch { }

    foreach ($model in @($ModelEmbed, $ModelChat)) {
        $base = ($model -split ":")[0]
        if ($installed -contains $base) {
            Write-Info "$model is already present"
        } elseif (Test-Have ollama) {
            Write-Info "pulling $model (this is a multi-gigabyte download)"
            & ollama pull $model
            if ($LASTEXITCODE -ne 0) { Write-Warn "could not pull $model; pull it yourself later" }
        } else {
            Write-Warn "$model is missing and the ollama CLI is unavailable here."
            Write-Info "  pull it on the machine running Ollama:  ollama pull $model"
        }
    }
} elseif ($NoModels) {
    Write-Info "skipping model downloads (-NoModels)"
}

# ------------------------------------------------------- 6. qdrant

Write-Step "Setting up the vector store"

function Test-DockerUp {
    if (-not (Test-Have docker)) { return $false }
    & docker info 2>$null | Out-Null
    return ($LASTEXITCODE -eq 0)
}

$resolvedQdrant = $Qdrant
if ($resolvedQdrant -eq "auto") {
    if (Test-DockerUp) {
        $resolvedQdrant = "server"
    } else {
        $resolvedQdrant = "embedded"
        Write-Info "Docker is not running, so choosing embedded mode"
    }
}

if ($resolvedQdrant -eq "embedded") {
    # An empty URL makes the client run Qdrant on local disk.
    Set-EnvValue "FETCH_QDRANT_URL" ""
    Write-Info "Qdrant will run embedded on local disk (no Docker required)"
} else {
    if (-not (Test-DockerUp)) {
        Stop-WithError "-Qdrant server was requested but Docker is not running.`nStart Docker Desktop, or re-run with -Qdrant embedded."
    }
    Write-Info "starting Qdrant with docker compose"
    & docker compose up -d qdrant 2>$null | Out-Null
    if ($LASTEXITCODE -ne 0) { Stop-WithError "could not start Qdrant. Try: docker compose up -d qdrant" }
    Set-EnvValue "FETCH_QDRANT_URL" "http://localhost:6333"
    foreach ($i in 1..10) {
        if (Test-UrlAlive "http://localhost:6333/") { break }
        Start-Sleep -Seconds 2
    }
    if (Test-UrlAlive "http://localhost:6333/") { Write-Info "Qdrant is up at http://localhost:6333" }
    else { Write-Warn "Qdrant did not answer yet; it may still be starting" }
}

# ------------------------------------------------------- 7. verify

Write-Step "Verifying the installation"

Invoke-Project mcp-fetch-server doctor
if ($LASTEXITCODE -ne 0) {
    Write-Warn "some checks failed; see above. The install is still usable for whatever passed."
} else {
    Write-Info "all checks passed"
}

# ------------------------------------------------------- 8. corpus

if ($Corpus) {
    Write-Step "Ingesting $Corpus"
    if (Test-Path $Corpus) {
        Invoke-Project mcp-fetch-server ingest $Corpus --quiet
        Invoke-Project mcp-fetch-server embed --quiet
    } else {
        Write-Warn "no such folder: $Corpus"
    }
}

# ------------------------------------------------------- 9. watcher

if ($Watch) {
    Write-Step "Registering the folder watcher"
    $watchTarget = if ($Corpus) { $Corpus } else { Join-Path $projectDir "corpus" }
    New-Item -ItemType Directory -Force -Path $watchTarget | Out-Null
    $exe = Join-Path $projectDir ".venv\Scripts\mcp-fetch-server.exe"

    if (-not (Test-Path $exe)) {
        Write-Warn "console script not found at $exe; skipping the scheduled task"
    } else {
        try {
            # A scheduled task is the Windows equivalent of the systemd unit
            # used on Linux: it starts at logon and restarts on failure.
            $action = New-ScheduledTaskAction -Execute $exe `
                -Argument "watch `"$watchTarget`" --interval 60 --quiet" `
                -WorkingDirectory $projectDir
            $trigger = New-ScheduledTaskTrigger -AtLogOn
            $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries `
                -DontStopIfGoingOnBatteries -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) `
                -ExecutionTimeLimit ([TimeSpan]::Zero)

            Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
                -Settings $settings -Description "Keeps the MCP fetch server corpus in step with a folder" `
                -Force | Out-Null
            Start-ScheduledTask -TaskName $TaskName
            Write-Info "registered scheduled task: $TaskName"
            Write-Info "watching: $watchTarget"
            Write-Info "manage it with:  Get-ScheduledTask -TaskName '$TaskName'"
        } catch {
            Write-Warn "could not register the scheduled task: $($_.Exception.Message)"
            Write-Info "run it in the foreground instead:"
            Write-Info "  uv run mcp-fetch-server watch `"$watchTarget`""
        }
    }
}

# ------------------------------------------------------- done

$corpusHint = if ($Corpus) { $Corpus } else { ".\corpus" }
Write-Host ""
Write-Host "Done." -ForegroundColor Green -NoNewline
Write-Host " Installed at $projectDir"
Write-Host @"

Next steps:

  # add documents and make them searchable
  uv run mcp-fetch-server ingest $corpusHint
  uv run mcp-fetch-server enrich
  uv run mcp-fetch-server embed
  uv run mcp-fetch-server search "your question" --answer

  # run it for an MCP client such as Cursor
  uv run mcp-fetch-server serve --transport stdio

Config is in $projectDir\.env. Re-run this script any time; it will
not overwrite it.
"@
