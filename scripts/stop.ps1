param(
    [int]$Port = 8000,
    [string]$RunDir = 'D:\llm\runtime'
)

# Stop supervisor + service + engine (in that order). Idempotent.
# ASCII only - see the note in supervise.ps1.

$ErrorActionPreference = 'Continue'
$pidPath = Join-Path $RunDir 'watchdog.pid'
$stopFlag = Join-Path $RunDir 'stop.flag'

if (Test-Path $pidPath) {
    $supId = Get-Content $pidPath -ErrorAction SilentlyContinue
    if ($supId -and (Get-Process -Id $supId -ErrorAction SilentlyContinue)) {
        # Ask the supervisor to shut down cleanly (it kills the service itself).
        New-Item -ItemType File -Force -Path $stopFlag | Out-Null
        for ($i = 0; $i -lt 15; $i++) {
            Start-Sleep -Seconds 1
            if (-not (Get-Process -Id $supId -ErrorAction SilentlyContinue)) { break }
        }
        if (Get-Process -Id $supId -ErrorAction SilentlyContinue) {
            & taskkill /PID $supId /T /F 2>&1 | Out-Null
        }
        Write-Host "supervisor stopped (pid $supId)"
    }
    Remove-Item -Force $pidPath -ErrorAction SilentlyContinue
}

# Belt and braces: whatever still listens on the port goes down.
$listeners = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
foreach ($procId in ($listeners.OwningProcess | Sort-Object -Unique)) {
    & taskkill /PID $procId /T /F 2>&1 | Out-Null
    Write-Host "stopped service process $procId"
}

# Legacy safety net: the engine now runs inside the service process, but a
# llama-server left over from the old subprocess engine can still hold ~5 GB VRAM.
$engines = Get-Process -Name 'llama-server' -ErrorAction SilentlyContinue
foreach ($e in $engines) {
    & taskkill /PID $e.Id /T /F 2>&1 | Out-Null
    Write-Host "stopped engine process $($e.Id)"
}

Remove-Item -Force (Join-Path $RunDir 'stop.flag') -ErrorAction SilentlyContinue
Write-Host 'all stopped'
