param([switch]$Force)
$ErrorActionPreference='Stop'
$ProgressPreference='SilentlyContinue'
[Console]::OutputEncoding=[Text.Encoding]::UTF8
$env:PYTHONUTF8='1'
$root='D:\order\exchange\ledger-feed'
$lock=[IO.File]::Open((Join-Path $root 'snapshot-publish.lock'),[IO.FileMode]::OpenOrCreate,[IO.FileAccess]::ReadWrite,[IO.FileShare]::None)
try {
 if((Get-PSDrive D).Free -lt 8GB){throw 'Insufficient free space for a verified source snapshot'}
 $manifest=Join-Path $root 'current\manifest.json'
 $success=Join-Path $root 'snapshot-task-success.json'
 if((Test-Path $manifest) -and (Test-Path $success) -and -not $Force){
  $m=Get-Content $manifest -Raw -Encoding UTF8 | ConvertFrom-Json
  $s=Get-Content $success -Raw -Encoding UTF8 | ConvertFrom-Json
  if($m.snapshot_id -eq $s.snapshot_id -and ([DateTimeOffset]::UtcNow-[DateTimeOffset]::Parse($m.created_at)).TotalHours -lt 2){Write-Output 'SNAPSHOT_ALREADY_FRESH';return}
 }
 if(Test-Path $manifest){
  $before=Get-Content $manifest -Raw -Encoding UTF8 | ConvertFrom-Json
  if($before.snapshot_id -notmatch '^[0-9TZ]+$'){throw 'Unexpected snapshot identity'}
  $history=Join-Path $root 'manifests'
  New-Item -ItemType Directory -Path $history -Force | Out-Null
  Copy-Item -LiteralPath $manifest -Destination (Join-Path $history ($before.snapshot_id+'.json')) -Force
 }
 Set-Location 'D:\order\backend'
 $env:PYTHONPATH='D:\order\backend'
 $log='D:\order\logs\snapshot-'+(Get-Date -Format 'yyyyMMdd-HHmmss')+'.log'
 $ErrorActionPreference='Continue'
 & 'D:\order\backend\venv\Scripts\python.exe' -u -m app.snapshot 2>&1 | Tee-Object -FilePath $log
 $nativeExit=$LASTEXITCODE
 $ErrorActionPreference='Stop'
 if($nativeExit -ne 0){throw 'Source snapshot verification failed'}
 $next=Get-Content $manifest -Raw -Encoding UTF8 | ConvertFrom-Json
 if($next.schema_version -ne 'ledger-feed.v1' -or -not $next.through_seq){throw 'Invalid published feed contract'}
 @{snapshot_id=$next.snapshot_id;through_seq=$next.through_seq;at=[DateTimeOffset]::UtcNow.ToString('o');log=$log} | ConvertTo-Json | Set-Content -LiteralPath ($success+'.next') -Encoding UTF8
 Move-Item -LiteralPath ($success+'.next') -Destination $success -Force
 Write-Output ('SNAPSHOT_VERIFIED '+$next.snapshot_id)
} finally {$lock.Dispose()}


