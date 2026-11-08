param(
    [int]$Port = 8000,
    [string]$InferDir = 'D:\llm\infer',
    [string]$RunDir = 'D:\llm\runtime'
)

# Manual start. No logon task, no Windows service: you run this, the supervisor
# keeps the service alive until you run stop.ps1.
# ASCII only - see the note in supervise.ps1.

$ErrorActionPreference = 'Stop'
$pidPath = Join-Path $RunDir 'watchdog.pid'
New-Item -ItemType Directory -Force -Path $RunDir | Out-Null

if (Test-Path $pidPath) {
    $old = Get-Content $pidPath -ErrorAction SilentlyContinue
    if ($old -and (Get-Process -Id $old -ErrorAction SilentlyContinue)) {
        Write-Host "supervisor already running (pid $old). Use stop.ps1 first."
        exit 0
    }
    Remove-Item -Force $pidPath
}

$super = Join-Path $InferDir 'scripts\supervise.ps1'
# Use Windows PowerShell (5.1) - this machine has no `pwsh` on PATH.
$shell = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
$p = Start-Process -FilePath $shell `
    -ArgumentList '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $super, '-Port', $Port `
    -WindowStyle Hidden -PassThru
Write-Host "supervisor started (pid $($p.Id)), service will listen on http://127.0.0.1:$Port"

# Wait until healthy so the caller knows whether the start worked.
for ($i = 0; $i -lt 90; $i++) {
    Start-Sleep -Seconds 2
    try {
        $h = Invoke-RestMethod "http://127.0.0.1:$Port/healthz" -TimeoutSec 3
        if ($h.status -eq 'ok') {
            Write-Host "ready after $($i * 2 + 2)s: engine=$($h.engine.engine) model=$($h.engine.model)"
            exit 0
        }
    } catch { }
}
Write-Host 'service did not become healthy in 180s; check D:\llm\runtime\watchdog.log'
exit 1
