# 局域网入口的守护启动器。由计划任务 LedgerFront 在开机时以 SYSTEM 拉起。
# 入口听 192.168.0.155:8000，台账本身听 127.0.0.1:8000；入口挂了页面就打不开，所以秒级拉回。

$ErrorActionPreference = 'Continue'
$Root   = 'D:\ledger'
$Python = Join-Path $Root 'venv\Scripts\python.exe'
$LogDir = Join-Path $Root 'logs'

$env:LEDGER_HOME      = Join-Path $Root 'home'
$env:LEDGER_FRONT_HOST = '192.168.0.155'
$env:COPILOT_PORT     = '8020'
$env:PYTHONPATH       = Join-Path $Root 'app\ledger'
$env:PYTHONUTF8       = '1'
$env:PYTHONIOENCODING = 'utf-8'
$env:PYTHONUNBUFFERED = '1'

New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
Get-ChildItem $LogDir -Filter 'front-*.log' -ErrorAction SilentlyContinue |
  Where-Object { $_.LastWriteTime -lt (Get-Date).AddDays(-30) } |
  Remove-Item -Force -ErrorAction SilentlyContinue
Set-Location $Root

$backoff = 2
while ($true) {
  $log = Join-Path $LogDir ('front-' + (Get-Date -Format 'yyyyMMdd') + '.log')
  ('[' + (Get-Date -Format 'yyyy-MM-dd HH:mm:ss') + '] 启动局域网入口') | Add-Content -Path $log -Encoding UTF8
  $sw = [Diagnostics.Stopwatch]::StartNew()
  & cmd.exe /c ('"' + $Python + '" -m ledger.front >> "' + $log + '" 2>&1')
  $sw.Stop()
  if ($sw.Elapsed.TotalSeconds -gt 60) { $backoff = 2 } else { $backoff = [Math]::Min($backoff * 2, 30) }
  ('[' + (Get-Date -Format 'yyyy-MM-dd HH:mm:ss') + '] 入口退出，' + $backoff + ' 秒后重启') | Add-Content -Path $log -Encoding UTF8
  Start-Sleep -Seconds $backoff
}
