# Agent Worker 的守护启动器。由计划任务 LedgerWorker 在开机时以 SYSTEM 拉起。
# 领取台账派发的取证案件和员工提问；是否完成仍由台账 Checker 重新核对。

$ErrorActionPreference = 'Continue'
$Root   = 'D:\ledger'
$Python = Join-Path $Root 'agent-venv\Scripts\python.exe'
$LogDir = Join-Path $Root 'logs'
$Config = Join-Path $Root 'agent-worker\worker.json'

$env:PYTHONPATH       = (Join-Path $Root 'app\agent-worker') + ';' + (Join-Path $Root 'app\host-agent')
$env:PYTHONUTF8       = '1'
$env:PYTHONIOENCODING = 'utf-8'
$env:PYTHONUNBUFFERED = '1'

New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
Get-ChildItem $LogDir -Filter 'worker-2*.log' -ErrorAction SilentlyContinue |
  Where-Object { $_.LastWriteTime -lt (Get-Date).AddDays(-30) } |
  Remove-Item -Force -ErrorAction SilentlyContinue
Set-Location $Root

$backoff = 2
while ($true) {
  $log = Join-Path $LogDir ('worker-' + (Get-Date -Format 'yyyyMMdd') + '.log')
  ('[' + (Get-Date -Format 'yyyy-MM-dd HH:mm:ss') + '] 启动 Agent Worker') | Add-Content -Path $log -Encoding UTF8
  $sw = [Diagnostics.Stopwatch]::StartNew()
  & cmd.exe /c ('"' + $Python + '" -m ledger_agent.cli --config "' + $Config + '" >> "' + $log + '" 2>&1')
  $sw.Stop()
  if ($sw.Elapsed.TotalSeconds -gt 60) { $backoff = 2 } else { $backoff = [Math]::Min($backoff * 2, 60) }
  ('[' + (Get-Date -Format 'yyyy-MM-dd HH:mm:ss') + '] Worker 退出，' + $backoff + ' 秒后重启') | Add-Content -Path $log -Encoding UTF8
  Start-Sleep -Seconds $backoff
}
