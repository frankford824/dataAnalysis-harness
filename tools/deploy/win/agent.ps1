# 台账助手的守护启动器。由计划任务 LedgerAgent 在开机时以 SYSTEM 拉起。
# 助手会重启台账服务，也会在改完自己的代码后自行退出，这里负责秒级拉回。

$ErrorActionPreference = 'Continue'
$Root   = 'D:\ledger'
$Python = Join-Path $Root 'venv\Scripts\python.exe'
$LogDir = Join-Path $Root 'logs'

$env:LEDGER_HOME      = Join-Path $Root 'home'
$env:COPILOT_HOME     = Join-Path $Root 'home\agent'
$env:COPILOT_APP_DIR  = Join-Path $Root 'app'
$env:COPILOT_PORT     = '8020'
$env:PYTHONPATH       = Join-Path $Root 'agent'
$env:PYTHONUTF8       = '1'
$env:PYTHONIOENCODING = 'utf-8'
$env:PYTHONUNBUFFERED = '1'

New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
Get-ChildItem $LogDir -Filter 'agent-*.log' -ErrorAction SilentlyContinue |
  Where-Object { $_.LastWriteTime -lt (Get-Date).AddDays(-30) } |
  Remove-Item -Force -ErrorAction SilentlyContinue
Set-Location $Root

$backoff = 2
while ($true) {
  $log = Join-Path $LogDir ('agent-' + (Get-Date -Format 'yyyyMMdd') + '.log')
  ('[' + (Get-Date -Format 'yyyy-MM-dd HH:mm:ss') + '] 启动台账助手') | Add-Content -Path $log -Encoding UTF8
  $sw = [Diagnostics.Stopwatch]::StartNew()
  & cmd.exe /c ('"' + $Python + '" -m ledger_copilot.server >> "' + $log + '" 2>&1')
  $sw.Stop()
  if ($sw.Elapsed.TotalSeconds -gt 60) { $backoff = 2 } else { $backoff = [Math]::Min($backoff * 2, 60) }
  ('[' + (Get-Date -Format 'yyyy-MM-dd HH:mm:ss') + '] 助手退出，' + $backoff + ' 秒后重启') | Add-Content -Path $log -Encoding UTF8
  Start-Sleep -Seconds $backoff
}
