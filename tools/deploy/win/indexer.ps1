# One owner, bounded runtime, actual endpoint liveness (not just a PID).
param([switch]$ValidateOnly)
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
[Console]::OutputEncoding = [Text.Encoding]::UTF8

function Get-IndexerRestartReason([bool]$WasReady, [int]$Failures, [double]$ElapsedSeconds) {
  if ($Failures -lt 3) { return '' }
  if ($WasReady) { return 'health endpoint failed three consecutive probes' }
  if ($ElapsedSeconds -ge 180) { return 'startup exceeded 180 seconds without a healthy endpoint' }
  return ''
}
function Get-IndexerNasRoot($Mappings) {
  # A disconnected global Z: must not shadow healthy X:.
  $mapping = @($Mappings | Where-Object {
    $_.RemotePath.TrimEnd('\') -ieq '\\192.168.0.125\dataAnalysis' -and [int]$_.Status -eq 0
  } | Sort-Object @{Expression={if ($_.LocalPath -ieq 'X:') {0} else {1}}} | Select-Object -First 1)
  $drive = if ($mapping.Count) { $mapping[0].LocalPath.TrimEnd('\') } else { 'X:' }
  # ASCII source also works in Windows PowerShell 5.1 without a UTF-8 BOM.
  $leaf = -join ([char[]](0x53f0,0x8d26,0x7cfb,0x7edf))
  return $drive + '\' + $leaf
}
if ($ValidateOnly) { return }

$Root = 'D:\ledger'
$Exe = Join-Path $Root 'bin\LedgerIndexer.exe'
$Data = Join-Path $Root 'index'
$LogDir = Join-Path $Root 'logs'
$mutex = New-Object Threading.Mutex($false,'Global\LedgerIndexerSupervisor')
try { $owned = $mutex.WaitOne(0) }
catch [Threading.AbandonedMutexException] { $owned = $true }
if (-not $owned) { throw 'Another indexer supervisor owns this service' }
$child = $null
try {
  New-Item -ItemType Directory -Force -Path $Data,$LogDir | Out-Null
  Set-Location $Root
  $env:TOKIO_WORKER_THREADS = '2'
  $backoff = 2
  while ($true) {
    $log = Join-Path $LogDir ('indexer-' + (Get-Date -Format 'yyyyMMdd') + '.log')
    ('[' + (Get-Date -Format o) + '] supervisor probing configured NAS') | Add-Content -LiteralPath $log -Encoding UTF8
    $nas = if ($env:LEDGER_NAS_ROOT) { $env:LEDGER_NAS_ROOT } else { Get-IndexerNasRoot @() }
    # Never enumerate stale global mappings in the service's hot path. A NAS
    # outage waits here without deleting files; another drive is configured
    # explicitly with LEDGER_NAS_ROOT, not silently selected from a stale alias.
    if (-not (Test-Path -LiteralPath $Exe -PathType Leaf)) {
      ('[' + (Get-Date -Format o) + '] executable missing') | Add-Content -LiteralPath $log -Encoding UTF8
      Start-Sleep -Seconds 30
      continue
    }
    if (-not (Test-Path -LiteralPath $nas -PathType Container)) {
      ('[' + (Get-Date -Format o) + '] NAS unreachable; no scan or removal') | Add-Content -LiteralPath $log -Encoding UTF8
      Start-Sleep -Seconds 30
      continue
    }
    $existing = @(Get-CimInstance Win32_Process -Filter "Name='LedgerIndexer.exe'" | Where-Object { $_.ExecutablePath -ieq $Exe })
    if ($existing.Count) {
      if ($existing.Count -ne 1 -or $existing[0].CommandLine -notmatch '--bind\s+"?127\.0\.0\.1:8765') {
        throw 'Ambiguous indexer ownership; refusing to stop or duplicate'
      }
      # If the old supervisor crashed, adopt its exact child under this mutex.
      # This covers an orphan that is still healthy as well as a wedged one.
      $child = Get-Process -Id $existing[0].ProcessId -ErrorAction Stop
      ('[' + (Get-Date -Format o) + '] adopted PID=' + $child.Id) | Add-Content -LiteralPath $log -Encoding UTF8
    } else {
      $stamp = Get-Date -Format 'yyyyMMdd-HHmmss-fff'
      $out = Join-Path $LogDir ('indexer-native-' + $stamp + '.out.log')
      $err = Join-Path $LogDir ('indexer-native-' + $stamp + '.err.log')
      $child = Start-Process -FilePath $Exe -ArgumentList @('serve','--root',('"' + $nas + '"'),'--data',$Data,'--bind','127.0.0.1:8765') `
        -WindowStyle Hidden -PassThru -RedirectStandardOutput $out -RedirectStandardError $err
    }
    ('[' + (Get-Date -Format o) + '] started PID=' + $child.Id + ' root=' + $nas) | Add-Content -LiteralPath $log -Encoding UTF8
    $started = [Diagnostics.Stopwatch]::StartNew()
    $wasReady = $false
    $failures = 0
    while (-not $child.HasExited) {
      Start-Sleep -Seconds 15
      $child.Refresh()
      if ($child.HasExited) { break }
      try {
        $health = Invoke-RestMethod 'http://127.0.0.1:8765/health' -TimeoutSec 5
        # Root unreachable is not a hung server and must not trigger table removal.
        if ($health.server_alive -eq $false) { throw 'scanner runtime unavailable' }
        $wasReady = $true
        $failures = 0
      } catch { $failures += 1 }
      $reason = Get-IndexerRestartReason $wasReady $failures $started.Elapsed.TotalSeconds
      if ($reason) {
        ('[' + (Get-Date -Format o) + '] recovering PID=' + $child.Id + ': ' + $reason) | Add-Content -LiteralPath $log -Encoding UTF8
        Stop-Process -Id $child.Id -Force -ErrorAction SilentlyContinue
        $child.WaitForExit(10000) | Out-Null
        break
      }
    }
    $child.Refresh()
    ('[' + (Get-Date -Format o) + '] exited PID=' + $child.Id + ' code=' + $child.ExitCode) | Add-Content -LiteralPath $log -Encoding UTF8
    $child = $null
    if ($wasReady -and $started.Elapsed.TotalSeconds -gt 60) { $backoff = 2 }
    else { $backoff = [Math]::Min($backoff * 2,60) }
    Start-Sleep -Seconds $backoff
  }
} finally {
  if ($child -and -not $child.HasExited) { Stop-Process -Id $child.Id -Force -ErrorAction SilentlyContinue }
  $mutex.ReleaseMutex()
  $mutex.Dispose()
}
