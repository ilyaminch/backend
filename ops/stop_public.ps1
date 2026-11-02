$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot
$localRoot = Join-Path $repoRoot '.local'
'Public stand deliberately disabled' | Set-Content -LiteralPath (Join-Path $localRoot 'public.disabled')
$pidFile = Join-Path $localRoot 'ngrok-stand.pid'
if (Test-Path -LiteralPath $pidFile) {
    $agent = Get-Process -Id ([int](Get-Content -LiteralPath $pidFile)) -ErrorAction SilentlyContinue
    $expectedBinary = Join-Path (Split-Path -Parent $repoRoot) '.tools/ngrok/ngrok.exe'
    if ($agent -and $agent.ProcessName -eq 'ngrok' -and
        [IO.Path]::GetFullPath($agent.Path) -eq [IO.Path]::GetFullPath($expectedBinary)) {
        Stop-Process -Id $agent.Id
    }
}
Set-Location -LiteralPath $repoRoot
$compose = @('compose', '-p', 'mdp-review-stand', '--env-file', '.local/public.env', '-f', 'ops/compose.local.yml',
    '-f', 'ops/compose.ollama.yml', '-f', 'ops/compose.demo.yml', '-f', 'ops/compose.public.yml')
& docker @compose stop frontend backend db
if ($LASTEXITCODE -ne 0) { throw 'Could not stop public Compose services' }
Write-Output 'Public stand stopped; local development stand unchanged. No database volumes removed.'
