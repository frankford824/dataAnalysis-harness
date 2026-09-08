param(
  [Parameter(Mandatory=$true)][string]$Payload,
  [Parameter(Mandatory=$true)][string]$ExpectedVersion,
  [Parameter(Mandatory=$true)][string]$Version
)
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [Text.Encoding]::UTF8
if ($Version -notmatch '^[0-9a-f]{7,40}$') { throw 'Invalid release version' }
$AppRoot = [IO.Path]::GetFullPath('D:\ledger\app')
$ReleaseRoot = [IO.Path]::GetFullPath((Join-Path 'D:\ledger\releases' $Version))
if (-not $ReleaseRoot.StartsWith('D:\ledger\releases\',[StringComparison]::OrdinalIgnoreCase)) { throw 'Release path outside workspace' }
$CurrentVersion = (Get-Content -LiteralPath (Join-Path $AppRoot 'VERSION') -Raw).Trim()
if ($CurrentVersion -ne $ExpectedVersion) { throw "Production version changed: $CurrentVersion" }
Add-Type -AssemblyName System.IO.Compression.FileSystem
$Zip = [IO.Compression.ZipFile]::OpenRead($Payload)
try {
  foreach ($Entry in $Zip.Entries) {
    if ($Entry.FullName.EndsWith('/')) { continue }
    $Name = $Entry.FullName.Replace('\','/')
    if ($Name -eq 'manifest.json') { continue }
    if ($Name -ne 'VERSION' -and -not $Name.StartsWith('ledger/ledger/')) { throw "Unexpected file: $Name" }
    $Destination = [IO.Path]::GetFullPath((Join-Path $AppRoot $Name))
    if (-not $Destination.StartsWith($AppRoot + '\',[StringComparison]::OrdinalIgnoreCase)) { throw "Unsafe destination: $Name" }
  }
} finally { $Zip.Dispose() }
New-Item -ItemType Directory -Force -Path $ReleaseRoot | Out-Null
$Stage = Join-Path $ReleaseRoot 'payload'
if (Test-Path -LiteralPath $Stage) { throw 'Release stage already exists; inspect it before retrying' }
[IO.Compression.ZipFile]::ExtractToDirectory($Payload,$Stage)
$Manifest = Get-Content -LiteralPath (Join-Path $Stage 'manifest.json') -Raw -Encoding UTF8 | ConvertFrom-Json
foreach ($File in $Manifest.files) {
  $Source = [IO.Path]::GetFullPath((Join-Path $Stage $File.path))
  $Target = [IO.Path]::GetFullPath((Join-Path $AppRoot $File.path))
  if (-not $Source.StartsWith($Stage+'\',[StringComparison]::OrdinalIgnoreCase) -or
      -not $Target.StartsWith($AppRoot+'\',[StringComparison]::OrdinalIgnoreCase)) { throw 'Manifest path outside workspace' }
  if ((Get-FileHash -LiteralPath $Source -Algorithm SHA256).Hash.ToLower() -ne $File.sha256) { throw "Payload hash mismatch: $($File.path)" }
  if ($File.is_new -and $File.path.EndsWith('.py') -and (Test-Path -LiteralPath $Target)) { throw "New module already exists on production: $($File.path)" }
  if ($File.before_sha256 -and (Test-Path -LiteralPath $Target)) {
    if ((Get-FileHash -LiteralPath $Target -Algorithm SHA256).Hash.ToLower() -ne $File.before_sha256) { throw "Production file changed: $($File.path)" }
  }
}
$Backup = Join-Path $ReleaseRoot 'before.zip'
$Archive = [IO.Compression.ZipFile]::Open($Backup,[IO.Compression.ZipArchiveMode]::Create)
try {
  foreach ($File in $Manifest.files) {
    $Target = Join-Path $AppRoot $File.path
    if (Test-Path -LiteralPath $Target) { [IO.Compression.ZipFileExtensions]::CreateEntryFromFile($Archive,$Target,$File.path) | Out-Null }
  }
} finally { $Archive.Dispose() }

function Stop-LedgerOnly {
  Stop-ScheduledTask -TaskName 'LedgerHarness'
  for ($Attempt=0; $Attempt -lt 20; $Attempt++) {
    $Listener = Get-NetTCPConnection -State Listen -LocalPort 8000 -ErrorAction SilentlyContinue | Select-Object -First 1
    if (-not $Listener) { return }
    $Process = Get-CimInstance Win32_Process -Filter ("ProcessId=" + $Listener.OwningProcess)
    if ($Process.CommandLine -notmatch 'ledger\.api:app') { throw 'Port 8000 belongs to another application' }
    Stop-Process -Id $Listener.OwningProcess -Force
    Start-Sleep -Milliseconds 250
  }
  throw 'Ledger did not stop'
}
function Start-AndCheck {
  Start-ScheduledTask -TaskName 'LedgerHarness'
  for ($Attempt=0; $Attempt -lt 60; $Attempt++) {
    try {
      $Response = Invoke-WebRequest -Uri 'http://127.0.0.1:8000/api/commission-v2/status' -TimeoutSec 5 -UseBasicParsing
      if ($Response.StatusCode -eq 200) { return }
    } catch { }
    Start-Sleep -Milliseconds 500
  }
  throw 'New commission API did not become ready'
}
Stop-LedgerOnly
$ModelHashes = @{}
Get-ChildItem -LiteralPath (Join-Path $AppRoot 'models\cn-ecommerce') -File | ForEach-Object {
  $ModelHashes[$_.Name] = (Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash
}
try {
  foreach ($File in $Manifest.files) {
    $Target = Join-Path $AppRoot $File.path
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $Target) | Out-Null
    Copy-Item -LiteralPath (Join-Path $Stage $File.path) -Destination $Target -Force
  }
  foreach ($Name in $ModelHashes.Keys) {
    if ((Get-FileHash -LiteralPath (Join-Path $AppRoot ('models\cn-ecommerce\'+$Name)) -Algorithm SHA256).Hash -ne $ModelHashes[$Name]) { throw "Model changed during code release: $Name" }
  }
  Start-AndCheck
  Write-Output ("DEPLOYED " + $Version + " BACKUP " + $Backup)
} catch {
  $Failure = $_
  Stop-LedgerOnly
  $Restore = Join-Path $ReleaseRoot 'restore'
  [IO.Compression.ZipFile]::ExtractToDirectory($Backup,$Restore)
  foreach ($File in $Manifest.files) {
    $OldFile = Join-Path $Restore $File.path
    if (Test-Path -LiteralPath $OldFile) { Copy-Item -LiteralPath $OldFile -Destination (Join-Path $AppRoot $File.path) -Force }
  }
  Start-ScheduledTask -TaskName 'LedgerHarness'
  throw $Failure
}
