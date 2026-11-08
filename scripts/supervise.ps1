param(
    [int]$Port = 8000,
    [int]$IntervalSec = 5,
    [int]$FailThreshold = 3,
    [int]$StartTimeoutSec = 180,
    [string]$WorkDir = 'D:\llm\infer',
    [string]$RunDir = 'D:\llm\runtime'
)

# Supervisor for the DSH inference service.
# Design choice: NO boot autostart. The user starts this manually; from then on
# it restarts the service whenever health checks fail. Stop it with stop.ps1.
#
# ASCII only on purpose: Windows PowerShell 5.1 reads BOM-less .ps1 as GBK and
# Chinese comments then swallow the next line (learned the hard way).

$ErrorActionPreference = 'Continue'
$logPath = Join-Path $RunDir 'watchdog.log'
$pidPath = Join-Path $RunDir 'watchdog.pid'
$stopFlag = Join-Path $RunDir 'stop.flag'
$outLog = Join-Path $RunDir 'service.log'
$errLog = Join-Path $RunDir 'service.err.log'

function Write-Log([string]$msg) {
    $line = '{0} {1}' -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $msg
    try { Add-Content -Path $logPath -Value $line -Encoding utf8 } catch { }
    Write-Host $line
}

function Rotate-Log {
    if ((Test-Path $logPath) -and ((Get-Item $logPath).Length -gt 5MB)) {
        Move-Item -Force $logPath "$logPath.1"
    }
}

function Test-Health {
    try {
        $r = Invoke-RestMethod "http://127.0.0.1:$Port/healthz" -TimeoutSec 3
        return ($r.status -eq 'ok')
    } catch { return $false }
}

function Wait-Healthy([int]$timeoutSec) {
    # Model load takes ~10 s, the first cold start can take 30 s. During this
    # window health failures are expected and must NOT count towards restart.
    $deadline = (Get-Date).AddSeconds($timeoutSec)
    while ((Get-Date) -lt $deadline) {
        if (Test-Health) { return $true }
        Start-Sleep -Seconds 2
    }
    return $false
}

function Stop-ServiceTree {
    # Kill whatever listens on the port (uvicorn), its children, and the
    # launcher we started (uv). /T also takes the python worker with it.
    $listeners = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
    foreach ($procId in ($listeners.OwningProcess | Sort-Object -Unique)) {
        & taskkill /PID $procId /T /F 2>&1 | Out-Null
    }
    if ($script:Launcher -and -not $script:Launcher.HasExited) {
        & taskkill /PID $script:Launcher.Id /T /F 2>&1 | Out-Null
    }
    $script:Launcher = $null
}

function Start-ServiceTree {
    $p = Start-Process -FilePath 'uv' `
        -ArgumentList 'run', 'uvicorn', 'dshinfer.api:app', '--port', $Port `
        -WorkingDirectory $WorkDir -WindowStyle Hidden -PassThru `
        -RedirectStandardOutput $outLog -RedirectStandardError $errLog
    $script:Launcher = $p
    return $p
}

if (Test-Path $stopFlag) { Remove-Item -Force $stopFlag }
$PID | Set-Content -Path $pidPath -Encoding ascii
Write-Log "supervisor up pid=$PID port=$Port interval=${IntervalSec}s threshold=$FailThreshold"

$script:Launcher = $null

# Bring the service up once, waiting for the model to load.
if (Test-Health) {
    Write-Log 'service already healthy'
}
else {
    Write-Log 'starting service'
    $null = Start-ServiceTree
    if (Wait-Healthy $StartTimeoutSec) { Write-Log 'service healthy' }
    else { Write-Log "service did not become healthy in ${StartTimeoutSec}s" }
}

$fails = 0
while ($true) {
    Rotate-Log
    if (Test-Path $stopFlag) {
        Write-Log 'stop flag found, shutting down'
        Stop-ServiceTree
        Remove-Item -Force $stopFlag
        break
    }

    if (Test-Health) {
        if ($fails -gt 0) { Write-Log 'health recovered' }
        $fails = 0
    }
    else {
        $fails++
        Write-Log "health check failed ($fails/$FailThreshold)"
        if ($fails -ge $FailThreshold) {
            Write-Log 'restarting service'
            Stop-ServiceTree
            Start-Sleep -Seconds 2
            $null = Start-ServiceTree
            $fails = 0
            if (Wait-Healthy $StartTimeoutSec) { Write-Log 'service healthy after restart' }
            else { Write-Log "restart did not become healthy in ${StartTimeoutSec}s" }
        }
    }
    Start-Sleep -Seconds $IntervalSec
}

Remove-Item -Force $pidPath -ErrorAction SilentlyContinue
Write-Log 'supervisor stopped'
