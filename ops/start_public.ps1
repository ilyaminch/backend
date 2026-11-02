param([switch]$Watch)
$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot
$workspaceRoot = Split-Path -Parent $repoRoot
$localRoot = Join-Path $repoRoot '.local'
$disabledFile = Join-Path $localRoot 'public.disabled'
if ($Watch -and (Test-Path -LiteralPath $disabledFile)) { exit 0 }
if (-not $Watch -and (Test-Path -LiteralPath $disabledFile)) {
    Remove-Item -LiteralPath $disabledFile
}
$runLock = $null
try {
    try {
        $runLock = [IO.File]::Open((Join-Path $localRoot 'public-watch.lock'), 'OpenOrCreate', 'ReadWrite', 'None')
    } catch [IO.IOException] { exit 0 }
    Set-Location -LiteralPath $repoRoot
    $originLine = Get-Content -LiteralPath (Join-Path $localRoot 'public.env') |
        Where-Object { $_ -match '^PUBLIC_ORIGIN=' } | Select-Object -First 1
    $origin = $originLine.Substring('PUBLIC_ORIGIN='.Length).Trim()
    $originUri = [uri]$origin
    if ($originUri.Scheme -ne 'https' -or $originUri.AbsolutePath -ne '/' -or
        $originUri.UserInfo -or $originUri.Query -or $originUri.Fragment) {
        throw 'Invalid HTTPS PUBLIC_ORIGIN'
    }
    $env:PUBLIC_ORIGIN = $origin
    $compose = @('compose', '-p', 'mdp-review-stand', '--env-file', '.local/public.env', '-f', 'ops/compose.local.yml',
        '-f', 'ops/compose.ollama.yml', '-f', 'ops/compose.demo.yml', '-f', 'ops/compose.public.yml')
    & docker info --format '{{.ServerVersion}}' *> $null
    if ($LASTEXITCODE -ne 0) {
        if (-not (Get-Process -Name 'Docker Desktop' -ErrorAction SilentlyContinue)) {
            Start-Process -FilePath "$env:ProgramFiles\Docker\Docker\Docker Desktop.exe" -WindowStyle Hidden
        }
        $dockerReady = $false
        for ($attempt = 0; $attempt -lt 60; $attempt++) {
            & docker info --format '{{.ServerVersion}}' *> $null
            if ($LASTEXITCODE -eq 0) { $dockerReady = $true; break }
            Start-Sleep -Seconds 2
        }
        if (-not $dockerReady) { throw 'Docker engine not ready' }
    }
    $backendId = & docker @compose ps -q backend
    $publicConfigured = $false
    if ($backendId) {
        # Read in memory only; never log the private runtime environment.
        $backendEnvironment = (& docker inspect --format '{{json .Config.Env}}' $backendId) | ConvertFrom-Json
        $publicConfigured = $backendEnvironment -contains "PUBLIC_ORIGIN=$origin" -and
            $backendEnvironment -contains 'COOKIE_SECURE=true' -and $backendEnvironment -contains 'APP_ENV=demo'
    }
    $ready = $false
    try {
        $response = Invoke-WebRequest -UseBasicParsing 'http://127.0.0.1:8088/api/v1/health/ready' -TimeoutSec 5
        $ready = $response.StatusCode -eq 200
    } catch {}
    if (-not $ready -or -not $publicConfigured) {
        if (-not $ready -and $publicConfigured) {
            & docker @compose restart backend frontend
            if ($LASTEXITCODE -ne 0) { throw 'Public service restart failed' }
        }
        & docker @compose up --detach --wait --wait-timeout 180 backend frontend
        if ($LASTEXITCODE -ne 0) { throw 'Public Compose startup failed' }
    }
    $ollamaReady = $false
    try { $null = Invoke-RestMethod 'http://127.0.0.1:11434/api/version' -TimeoutSec 3; $ollamaReady = $true } catch {}
    if (-not $ollamaReady -and -not (Get-Process -Name ollama -ErrorAction SilentlyContinue)) {
        Start-Process -FilePath "$env:LOCALAPPDATA\Programs\Ollama\ollama.exe" -ArgumentList 'serve' -WindowStyle Hidden `
            -RedirectStandardOutput (Join-Path $localRoot 'ollama-public.stdout.log') `
            -RedirectStandardError (Join-Path $localRoot 'ollama-public.stderr.log')
    }
    $tunnel = $null
    try {
        $tunnel = (Invoke-RestMethod 'http://127.0.0.1:4047/api/tunnels' -TimeoutSec 3).tunnels |
            Where-Object { $_.public_url -eq $origin -and $_.config.addr -eq 'http://127.0.0.1:8088' } |
            Select-Object -First 1
    } catch {}
    if (-not $tunnel) {
        $agent = $null
        $pidFile = Join-Path $localRoot 'ngrok-stand.pid'
        if (Test-Path -LiteralPath $pidFile) {
            $agent = Get-Process -Id ([int](Get-Content -LiteralPath $pidFile)) -ErrorAction SilentlyContinue
        }
        if ($agent -and $agent.ProcessName -eq 'ngrok') { throw 'Stand ngrok process exists but expected tunnel is unavailable' }
        $ngrok = Join-Path $workspaceRoot '.tools/ngrok/ngrok.exe'
        $credentialConfig = Join-Path $env:LOCALAPPDATA 'ngrok/ngrok.yml'
        $standConfig = Join-Path $localRoot 'ngrok-stand.yml'
        $arguments = @('http', 'http://127.0.0.1:8088', "--url=$origin",
            "--config=$credentialConfig,$standConfig", '--inspect=false',
            "--log=$(Join-Path $localRoot 'ngrok-stand.log')", '--log-format=json', '--log-level=warn')
        $agent = Start-Process -FilePath $ngrok -ArgumentList $arguments -WindowStyle Hidden -PassThru
        $agent.Id | Set-Content -LiteralPath $pidFile
        for ($attempt = 0; $attempt -lt 30; $attempt++) {
            try {
                $tunnel = (Invoke-RestMethod 'http://127.0.0.1:4047/api/tunnels' -TimeoutSec 2).tunnels |
                    Where-Object { $_.public_url -eq $origin } | Select-Object -First 1
                if ($tunnel) { break }
            } catch {}
            if ($agent.HasExited) { throw 'ngrok exited; inspect private ngrok-stand.log' }
            Start-Sleep -Seconds 1
        }
        if (-not $tunnel) { throw 'ngrok did not publish expected stable URL' }
    }
    Write-Output "Public stand ready: $origin"
    "$(Get-Date -Format o) OK" | Add-Content -LiteralPath (Join-Path $localRoot 'public-watch.log')
} catch {
    # Do not write private environment, request bodies, cookies or account passwords.
    "$(Get-Date -Format o) FAILED: $($_.Exception.Message)" |
        Add-Content -LiteralPath (Join-Path $localRoot 'public-watch.log')
    Write-Error $_.Exception.Message
    exit 1
} finally {
    if ($runLock) { $runLock.Dispose() }
}
