# Recover only Ledger's exact NAS indexer; never restart the machine or other services.
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
[Console]::OutputEncoding = [Text.Encoding]::UTF8
$Root = 'D:\ledger'
$Exe = Join-Path $Root 'bin\LedgerIndexer.exe'
$Backup = Join-Path $Root ('releases\indexer-recovery-' + (Get-Date -Format 'yyyyMMdd-HHmmss'))
if (-not (Test-Path -LiteralPath $Exe -PathType Leaf)) { throw 'Indexer executable missing' }
$task = Get-ScheduledTask -TaskName 'LedgerIndexer' -ErrorAction Stop
New-Item -ItemType Directory -Path $Backup -ErrorAction Stop | Out-Null
Export-ScheduledTask -TaskName 'LedgerIndexer' | Set-Content -LiteralPath (Join-Path $Backup 'task.xml') -Encoding UTF8
Copy-Item -LiteralPath (Join-Path $Root 'bin\indexer.ps1') -Destination $Backup
Copy-Item -LiteralPath $Exe -Destination $Backup
Stop-ScheduledTask -TaskName 'LedgerIndexer' -ErrorAction SilentlyContinue
$orphans = @(Get-CimInstance Win32_Process -Filter "Name='LedgerIndexer.exe'")
foreach ($child in $orphans) {
  if ($child.ExecutablePath -ine $Exe -or $child.CommandLine -notmatch '--bind\s+127\.0\.0\.1:8765') {
    throw ('Unrelated indexer process; refusing to stop PID ' + $child.ProcessId)
  }
  Stop-Process -Id $child.ProcessId -Force -ErrorAction Stop
}
Start-ScheduledTask -TaskName 'LedgerIndexer' -ErrorAction Stop
[pscustomobject]@{task='LedgerIndexer';backup=$Backup;stopped_pids=@($orphans.ProcessId);started=$true} | ConvertTo-Json -Compress
