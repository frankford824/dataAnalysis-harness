param(
  [Parameter(Mandatory=$true)][string]$Payload,
  [Parameter(Mandatory=$true)][string]$ExpectedEngineVersion,
  [Parameter(Mandatory=$true)][string]$Version
)
$ErrorActionPreference='Stop'
$ProgressPreference='SilentlyContinue'
[Console]::OutputEncoding=[Text.Encoding]::UTF8
if($Version -notmatch '^[0-9a-f]{7,40}$'){throw 'Invalid UI revision'}
$AppRoot=[IO.Path]::GetFullPath('D:\ledger\app')
$StaticRoot=[IO.Path]::GetFullPath((Join-Path $AppRoot 'ledger\ledger\static'))
$ReleaseRoot=[IO.Path]::GetFullPath((Join-Path 'D:\ledger\releases' ('ui-'+$Version)))
if(-not $ReleaseRoot.StartsWith('D:\ledger\releases\',[StringComparison]::OrdinalIgnoreCase)){throw 'Unsafe release root'}
function Hash([string]$Path){return (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()}
function Static-Path([string]$Relative){
  if(-not $Relative.StartsWith('ledger/ledger/static/',[StringComparison]::Ordinal) -or $Relative -match '(^|/)\.\.(/|$)' -or $Relative.Contains('\')){throw "Not a static UI path: $Relative"}
  $Target=[IO.Path]::GetFullPath((Join-Path $AppRoot $Relative))
  if(-not $Target.StartsWith($StaticRoot+'\',[StringComparison]::OrdinalIgnoreCase)){throw 'Static target escaped root'}
  if([IO.Path]::GetExtension($Target) -notin @('.html','.js','.css','.json','.svg','.woff','.woff2','.ttf','.png','.webp','.jpg')){throw 'Unexpected static file type'}
  return $Target
}
function Protected-Path([string]$Relative){
  if($Relative -ne 'VERSION' -and -not ($Relative.StartsWith('ledger/ledger/') -and $Relative.EndsWith('.py')) -and -not $Relative.StartsWith('models/')){throw 'Unexpected protected path'}
  $Target=[IO.Path]::GetFullPath((Join-Path $AppRoot $Relative))
  if(-not $Target.StartsWith($AppRoot+'\',[StringComparison]::OrdinalIgnoreCase)){throw 'Protected path escaped root'}
  return $Target
}
function Check-Engine($Manifest){
  if((Get-Content -LiteralPath (Join-Path $AppRoot 'VERSION') -Raw).Trim() -ne $ExpectedEngineVersion){throw 'Engine stamp changed'}
  $Listener=Get-NetTCPConnection -State Listen -LocalPort 8000 | Select-Object -First 1
  if(-not $Listener -or $Listener.OwningProcess -ne [int]$Manifest.expected_pid){throw 'The running ledger process changed'}
  $Owner=Get-CimInstance Win32_Process -Filter ('ProcessId='+$Listener.OwningProcess)
  if($Owner.CommandLine -notmatch 'ledger\.api:app'){throw 'Port 8000 is not the ledger service'}
  $CodeFiles=@(Get-ChildItem -LiteralPath (Join-Path $AppRoot 'ledger\ledger') -Recurse -File -Filter '*.py')
  $ModelFiles=@(Get-ChildItem -LiteralPath (Join-Path $AppRoot 'models') -Recurse -File | Where-Object {$_.FullName -notmatch '\\__pycache__\\'})
  if((1+$CodeFiles.Count+$ModelFiles.Count) -ne @($Manifest.protected_files).Count){throw 'Protected engine/model inventory changed'}
  foreach($File in $Manifest.protected_files){if((Hash (Protected-Path $File.path)) -ne $File.sha256){throw "Protected engine/model file changed: $($File.path)"}}
  $Info=Invoke-RestMethod -Uri 'http://127.0.0.1:8000/api/version' -TimeoutSec 30
  if($Info.version -ne $ExpectedEngineVersion -or $Info.model_revision -ne $Manifest.model_revision){throw 'Engine or model revision changed'}
}
Add-Type -AssemblyName System.IO.Compression.FileSystem
$Lock=[IO.File]::Open('D:\ledger\releases\ui-release.lock',[IO.FileMode]::OpenOrCreate,[IO.FileAccess]::ReadWrite,[IO.FileShare]::None)
try{
  if(Test-Path -LiteralPath $ReleaseRoot){throw 'UI release directory already exists; inspect before retrying'}
  $Zip=[IO.Compression.ZipFile]::OpenRead($Payload)
  try{
    $Seen=New-Object 'System.Collections.Generic.HashSet[string]' ([StringComparer]::OrdinalIgnoreCase)
    foreach($Entry in $Zip.Entries){if($Entry.FullName.EndsWith('/')){throw 'Directory entries are not expected in a UI payload'};if(-not $Seen.Add($Entry.FullName)){throw 'Duplicate archive path'};if($Entry.FullName -ne 'manifest.json'){$null=Static-Path $Entry.FullName}}
  }finally{$Zip.Dispose()}
  New-Item -ItemType Directory -Force -Path $ReleaseRoot | Out-Null
  $Stage=Join-Path $ReleaseRoot 'payload'
  [IO.Compression.ZipFile]::ExtractToDirectory($Payload,$Stage)
  $Manifest=Get-Content -LiteralPath (Join-Path $Stage 'manifest.json') -Raw -Encoding UTF8 | ConvertFrom-Json
  if($Manifest.schema -ne 'ledger-static-ui-v1' -or $Manifest.version -ne $Version -or $Manifest.engine_version -ne $ExpectedEngineVersion){throw 'Unexpected UI manifest'}
  if(@($Manifest.protected_files).Count -lt 2 -or @($Manifest.protected_files | Where-Object {$_.path -eq 'VERSION'}).Count -ne 1){throw 'Engine protection inventory is required'}
  Check-Engine $Manifest
  foreach($File in $Manifest.files){
    $Target=Static-Path $File.path
    $Source=[IO.Path]::GetFullPath((Join-Path $Stage $File.path))
    if(-not $Source.StartsWith($Stage+'\',[StringComparison]::OrdinalIgnoreCase) -or (Hash $Source) -ne $File.sha256){throw 'Invalid payload content'}
    if($File.before_sha256){if(-not (Test-Path -LiteralPath $Target) -or (Hash $Target) -ne $File.before_sha256){throw "UI asset changed: $($File.path)"}}
    elseif((Test-Path -LiteralPath $Target) -and (Hash $Target) -ne $File.sha256){throw 'Immutable asset name collision'}
  }
  $Index=@($Manifest.files | Where-Object {$_.path -eq 'ledger/ledger/static/index.html'})
  $UiStamp=@($Manifest.files | Where-Object {$_.path -eq 'ledger/ledger/static/ui-version.json'})
  if($Index.Count -ne 1 -or $UiStamp.Count -ne 1){throw 'UI index and UI stamp are required'}
  $Index=$Index[0];$UiStamp=$UiStamp[0]
  $IndexTarget=Static-Path $Index.path;$StampTarget=Static-Path $UiStamp.path
  $Backup=Join-Path $ReleaseRoot 'before.zip'
  $Archive=[IO.Compression.ZipFile]::Open($Backup,[IO.Compression.ZipArchiveMode]::Create)
  try{foreach($File in $Manifest.files){$Target=Static-Path $File.path;if(Test-Path -LiteralPath $Target){[IO.Compression.ZipFileExtensions]::CreateEntryFromFile($Archive,$Target,$File.path) | Out-Null}}}finally{$Archive.Dispose()}
  # Publish new immutable assets first. Existing clients keep every old hash.
  foreach($File in $Manifest.files){
    if($File.path -in @($Index.path,$UiStamp.path)){continue}
    $Target=Static-Path $File.path
    if(-not (Test-Path -LiteralPath $Target)){New-Item -ItemType Directory -Force -Path (Split-Path -Parent $Target) | Out-Null;Copy-Item -LiteralPath (Join-Path $Stage $File.path) -Destination $Target}
    if((Hash $Target) -ne $File.sha256){throw 'Static copy verification failed'}
  }
  if((Hash $IndexTarget) -ne $Index.before_sha256){throw 'UI index changed during staging'}
  Check-Engine $Manifest
  $PendingIndex=Join-Path $StaticRoot ('index.pending-'+$Version)
  $PendingStamp=Join-Path $StaticRoot ('ui-version.pending-'+$Version)
  Copy-Item -LiteralPath (Join-Path $Stage $Index.path) -Destination $PendingIndex
  Copy-Item -LiteralPath (Join-Path $Stage $UiStamp.path) -Destination $PendingStamp
  $Switched=$false
  try{
    [IO.File]::Replace($PendingIndex,$IndexTarget,(Join-Path $ReleaseRoot 'index.previous.html'));$Switched=$true
    if(Test-Path -LiteralPath $StampTarget){[IO.File]::Replace($PendingStamp,$StampTarget,(Join-Path $ReleaseRoot 'ui-version.previous.json'))}else{[IO.File]::Move($PendingStamp,$StampTarget)}
    if((Hash $IndexTarget) -ne $Index.sha256 -or (Hash $StampTarget) -ne $UiStamp.sha256){throw 'UI entry readback failed'}
    $Home=Invoke-WebRequest -Uri 'http://127.0.0.1:8000/' -UseBasicParsing -TimeoutSec 30
    $Published=Invoke-RestMethod -Uri ('http://127.0.0.1:8000/static/ui-version.json?release='+$Version) -TimeoutSec 30
    if($Home.StatusCode -ne 200 -or $Published.ui_version -ne $Version){throw 'Published UI did not read back'}
    Check-Engine $Manifest
    $Receipt=[pscustomobject]@{ui_version=$Version;engine_version=$ExpectedEngineVersion;model_revision=$Manifest.model_revision;pid=$Manifest.expected_pid;service_restarted=$false;protected_files=@($Manifest.protected_files).Count;index_sha256=$Index.sha256;backup=$Backup;published_at=[DateTime]::UtcNow.ToString('o')}
    $Receipt | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath (Join-Path $ReleaseRoot 'receipt.json') -Encoding UTF8
    $Receipt | ConvertTo-Json -Compress
  }catch{
    if($Switched){
      $Restore=Join-Path $ReleaseRoot 'restore';[IO.Compression.ZipFile]::ExtractToDirectory($Backup,$Restore)
      $RestoreIndex=Join-Path $StaticRoot ('index.restore-'+$Version);Copy-Item -LiteralPath (Join-Path $Restore $Index.path) -Destination $RestoreIndex
      [IO.File]::Replace($RestoreIndex,$IndexTarget,(Join-Path $ReleaseRoot 'index.failed.html'))
      $OldStamp=Join-Path $Restore $UiStamp.path
      if(Test-Path -LiteralPath $OldStamp){Copy-Item -LiteralPath $OldStamp -Destination $StampTarget -Force}else{if(Test-Path -LiteralPath $StampTarget){Remove-Item -LiteralPath $StampTarget}}
    }
    throw
  }
}finally{$Lock.Dispose()}
