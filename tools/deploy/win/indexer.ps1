# Tantivy/Parquet 索引服务守护器。没有 NAS 映射时只等待，绝不把“看不见根目录”解释成删除。

$ErrorActionPreference = 'Continue'
$Root = 'D:\ledger'
$Exe = Join-Path $Root 'bin\LedgerIndexer.exe'
$NasRoot = 'X:\台账系统'
$Data = Join-Path $Root 'index'
$LogDir = Join-Path $Root 'logs'

New-Item -ItemType Directory -Force -Path $Data,$LogDir | Out-Null
Set-Location $Root
$backoff = 2
while ($true) {
  $log = Join-Path $LogDir ('indexer-' + (Get-Date -Format 'yyyyMMdd') + '.log')
  if (-not (Test-Path -LiteralPath $Exe)) {
    ('[' + (Get-Date -Format s) + '] 找不到 ' + $Exe) | Add-Content $log -Encoding UTF8
    Start-Sleep -Seconds 30
    continue
  }
  if (-not (Test-Path -LiteralPath $NasRoot -PathType Container)) {
    ('[' + (Get-Date -Format s) + '] NAS 不可达，等待且不扫描、不撤表') | Add-Content $log -Encoding UTF8
    Start-Sleep -Seconds 30
    continue
  }
  ('[' + (Get-Date -Format s) + '] 启动 LedgerIndexer') | Add-Content $log -Encoding UTF8
  $sw = [Diagnostics.Stopwatch]::StartNew()
  & $Exe serve --root $NasRoot --data $Data --bind '127.0.0.1:8765' >> $log 2>&1
  $code = $LASTEXITCODE
  $sw.Stop()
  ('[' + (Get-Date -Format s) + '] 退出 code=' + $code) | Add-Content $log -Encoding UTF8
  if ($sw.Elapsed.TotalSeconds -gt 60) { $backoff = 2 } else { $backoff = [Math]::Min($backoff * 2, 60) }
  Start-Sleep -Seconds $backoff
}
