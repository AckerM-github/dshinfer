param(
    [Parameter(Position = 0)][string]$Command = 'help',
    [Parameter(Position = 1, ValueFromRemainingArguments = $true)][string[]]$Rest,
    [int]$Port = 8000,
    [string]$InferDir = '',
    [string]$RunDir = 'D:\llm\runtime',
    [switch]$Follow
)

# ops.ps1 - Windows-native counterpart of docker/ops.sh
#
# Same subcommand vocabulary on both machines, so the two deployment forms cannot
# drift into different mental models:
#
#   up [--build]   build (optional) + start in background, wait for /healthz
#   down | stop    stop supervisor + service + engine
#   start          start without stopping first
#   restart        stop then start
#   status         port / health / engine process / VRAM / git revision
#   check          numbered checks, exit 1 if any fails
#   logs [N]       tail service / engine / watchdog logs (-Follow to stream)
#   bench [args]   scripts/bench_load.py against the running service
#   models         list *.gguf with sizes and verify them against models.json
#   tui            Textual monitor for the native service
#   verify         emit /admin/config as JSON for cross-form comparison
#   fetch [args]   fetch-model.sh inside WSL (weights are shared with the container)
#
# ASCII only on purpose: Windows PowerShell 5.1 reads BOM-less .ps1 as GBK.

$ErrorActionPreference = 'Stop'
if (-not $InferDir) { $InferDir = Split-Path $PSScriptRoot -Parent }
$Base = "http://127.0.0.1:$Port"
# Weights dir: same precedence as the service (env > Windows default), so check
# inspects the directory the engine actually reads.
$WeightsDir = if ($env:DSHINFER_MODELS_DIR) { $env:DSHINFER_MODELS_DIR } else { "D:\llm\models\gguf" }
$EngineLog = Join-Path $RunDir 'llama-server.log'
$ServiceLog = Join-Path $RunDir 'service.log'
$WatchdogLog = Join-Path $RunDir 'watchdog.log'

function Head1([string]$Text) {
    Write-Host ''
    Write-Host ('== ' + $Text)
}

function Get-Health {
    try { return Invoke-RestMethod "$Base/healthz" -TimeoutSec 3 } catch { return $null }
}

function Get-ListenPid {
    $c = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
    if (-not $c) { return @() }
    return @($c.OwningProcess | Sort-Object -Unique)
}

function Get-Vram {
    try {
        $q = & nvidia-smi --query-gpu=memory.used,memory.total --format=csv,noheader,nounits 2>$null
        if (-not $q) { return $null }
        $parts = ($q -split ',')
        return [pscustomobject]@{ Used = [int]$parts[0].Trim(); Total = [int]$parts[1].Trim() }
    } catch { return $null }
}

function Get-Head {
    try { return (& git -C $InferDir rev-parse --short HEAD 2>$null) } catch { return 'unknown' }
}

function Format-Size([long]$Bytes) {
    if ($Bytes -ge 1GB) { return ('{0:N2} GiB' -f ($Bytes / 1GB)) }
    return ('{0:N0} MiB' -f ($Bytes / 1MB))
}

function cmd_status {
    Head1 'status'
    $h = Get-Health
    $pids = Get-ListenPid
    Write-Host ("  revision   : {0}" -f (Get-Head))
    Write-Host ("  port {0,-5} : {1}" -f $Port, $(if ($pids.Count) { 'listening (pid ' + ($pids -join ',') + ')' } else { 'not listening' }))
    if ($h) {
        $ready = $h.engine.ready
        Write-Host ("  healthz    : ok   engine={0} ready={1} model={2}" -f $h.engine.engine, $ready, $h.engine.model)
        if ($null -ne $h.engine.load_seconds) { Write-Host ("  load       : {0:N2} s" -f $h.engine.load_seconds) }
    } else {
        Write-Host '  healthz    : no answer (service down?)'
    }
    $eng = Get-Process -Name 'llama-server' -ErrorAction SilentlyContinue
    if ($eng) {
        $mb = [math]::Round(($eng | Measure-Object -Property WorkingSet64 -Sum).Sum / 1MB)
        Write-Host ("  engine     : llama-server pid {0} rss {1} MiB" -f (($eng | ForEach-Object { $_.Id }) -join ','), $mb)
    } else {
        Write-Host '  engine     : llama-server not running'
    }
    $vram = Get-Vram
    if ($vram) { Write-Host ("  vram       : {0} / {1} MiB" -f $vram.Used, $vram.Total) } else { Write-Host '  vram       : nvidia-smi unavailable' }
}

function cmd_check {
    Head1 'check'
    $fails = 0
    $i = 0
    function Row([string]$State, [string]$Text) {
        Write-Host ("  [{0}] {1}" -f $State, $Text)
    }
    $i++; $h = Get-Health
    if ($h -and $h.status -eq 'ok') { Row 'OK' ("$i. service healthz ok") } else { $fails++; Row 'FAIL' ("$i. service healthz") }
    $i++
    try {
        $m = Invoke-RestMethod "$Base/v1/models" -TimeoutSec 5
        Row 'OK' ("$i. /v1/models -> " + $m.data.Count + ' entries')
    } catch { $fails++; Row 'FAIL' "$i. /v1/models" }
    $i++
    if ($h -and $h.engine.ready -eq $true) {
        Row 'OK' ("$i. engine ready, model=" + $h.engine.model)
    } else { $fails++; Row 'FAIL' "$i. engine not ready" }
    $i++
    $modelsDir = $WeightsDir
    $ggufs = @(Get-ChildItem -Path $modelsDir -Filter '*.gguf' -ErrorAction SilentlyContinue)
    if ($ggufs.Count -gt 0) { $fails = $fails; Row 'OK' ("$i. weights: " + $ggufs.Count + ' gguf in ' + $modelsDir) } else { $fails++; Row 'FAIL' ("$i. weights dir empty: " + $modelsDir) }
    $i++
    $manifest = Join-Path $InferDir 'models\models.json'
    if (Test-Path $manifest) {
        $bad = @()
        $spec = Get-Content $manifest -Raw -Encoding UTF8 | ConvertFrom-Json
        foreach ($entry in $spec.models) {
            if (-not $entry.file) { continue }
            $p = Join-Path $modelsDir $entry.file
            if (-not (Test-Path $p)) { $bad += ($entry.file + ' missing'); continue }
            $len = (Get-Item $p).Length
            if ($entry.size_bytes -and ($len -ne [long]$entry.size_bytes)) {
                $bad += ('{0} size {1} != {2}' -f $entry.file, $len, $entry.size_bytes)
            }
        }
        if ($bad.Count -eq 0) { Row 'OK' "$i. models.json sizes match" } else { $fails++; Row 'FAIL' ("$i. " + ($bad -join '; ')) }
    } else { Row 'WARN' "$i. models.json not found, skipped" }
    $i++
    $vram = Get-Vram
    if ($vram) {
        $pct = [math]::Round(100.0 * $vram.Used / $vram.Total, 1)
        Row 'OK' ("$i. vram {0}/{1} MiB ({2}%)" -f $vram.Used, $vram.Total, $pct)
    } else { Row 'WARN' "$i. nvidia-smi unavailable" }
    $i++
    if ((Get-ListenPid).Count -gt 0) { Row 'OK' "$i. port $Port owned by a live process" } else { $fails++; Row 'FAIL' "$i. nothing listening on $Port" }
    Head1 ("result: {0} item(s) failed" -f $fails)
    if ($fails -gt 0) { exit 1 }
}

function cmd_logs([string[]]$Args) {
    Head1 'logs'
    $n = 40
    $which = 'service'
    foreach ($a in $Args) {
        if ($a -match '^\d+$') { $n = [int]$a }
        elseif ($a -in @('service', 'engine', 'watchdog')) { $which = $a }
    }
    $path = switch ($which) {
        'engine' { $EngineLog }
        'watchdog' { $WatchdogLog }
        default { $ServiceLog }
    }
    if (-not (Test-Path $path)) { Write-Host ("  no such log: " + $path); exit 1 }
    if ($Follow) { Get-Content -Path $path -Tail $n -Wait }
    else { Get-Content -Path $path -Tail $n }
}

function cmd_models {
    Head1 'models'
    $dir = $WeightsDir
    Get-ChildItem -Path $dir -Filter '*.gguf' -ErrorAction SilentlyContinue |
        Sort-Object Length -Descending |
        ForEach-Object { Write-Host ("  {0,-72} {1}" -f $_.Name, (Format-Size $_.Length)) }
}

function Invoke-Repo([string[]]$ScriptArgs) {
    # $Args is an automatic variable: naming a parameter $Args swallows every
    # positional value after the first one, so verify/bench/tui silently ran
    # python with no arguments (it dropped into the REPL).
    $py = Join-Path $InferDir '.venv\Scripts\python.exe'
    Push-Location $InferDir
    try {
        if (Test-Path $py) { & $py @ScriptArgs } else { & uv run @ScriptArgs }
    } finally { Pop-Location }
}

function cmd_verify {
    Head1 'verify'
    $out = Join-Path $InferDir '.verify'
    New-Item -ItemType Directory -Force -Path $out | Out-Null
    $stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
    $file = Join-Path $out ('host-' + $stamp + '.json')
    Invoke-Repo @('scripts/verify_env.py', '--base', $Base, '--json-out', $file)
    Write-Host ("  wrote " + $file)
    Write-Host '  compare with the container report: python scripts/verify_env.py --compare <a.json> <b.json>'
}

switch ($Command.ToLower()) {
    'up' {
        Head1 'up (native)'
        & (Join-Path $InferDir 'scripts\start.ps1') -Port $Port -InferDir $InferDir -RunDir $RunDir
    }
    'down' { Head1 'down (native)'; & (Join-Path $InferDir 'scripts\stop.ps1') -Port $Port -RunDir $RunDir }
    'stop' { Head1 'stop (native)'; & (Join-Path $InferDir 'scripts\stop.ps1') -Port $Port -RunDir $RunDir }
    'start' {
        Head1 'start (native)'
        & (Join-Path $InferDir 'scripts\start.ps1') -Port $Port -InferDir $InferDir -RunDir $RunDir
    }
    'restart' {
        Head1 'restart (native)'
        & (Join-Path $InferDir 'scripts\stop.ps1') -Port $Port -RunDir $RunDir
        & (Join-Path $InferDir 'scripts\start.ps1') -Port $Port -InferDir $InferDir -RunDir $RunDir
    }
    'status' { cmd_status }
    'check' { cmd_check }
    'logs' { cmd_logs $Rest }
    'models' { cmd_models }
    'bench' {
        Head1 'bench (native)'
        $bench = @('scripts/bench_load.py', '--service', $Base)
        if ($Rest) { $bench += $Rest }
        Invoke-Repo $bench
    }
    'tui' {
        Head1 'tui (native)'
        Invoke-Repo @('-m', 'dshinfer.tui', '--url', $Base)
    }
    'verify' { cmd_verify }
    'fetch' {
        Head1 'fetch model (WSL shares the weights folder)'
        & wsl.exe -d Ubuntu -- bash (Join-Path (Split-Path $InferDir -Parent) 'infer\scripts\fetch-model.sh') @Rest
    }
    default {
        Write-Host 'usage: scripts\ops.ps1 <up|down|stop|start|restart|status|check|logs|bench|models|tui|verify|fetch> [-Port 8000] [-Follow]'
        Write-Host ''
        Write-Host '  same subcommands as docker/ops.sh, so the native and container forms stay symmetric:'
        Write-Host '    ops.ps1 status   <->  docker/ops.sh status'
        Write-Host '    ops.ps1 check    <->  docker/ops.sh check'
        Write-Host '    ops.ps1 bench    <->  docker/ops.sh bench'
        Write-Host '    ops.ps1 verify   <->  docker/ops.sh verify'
    }
}
