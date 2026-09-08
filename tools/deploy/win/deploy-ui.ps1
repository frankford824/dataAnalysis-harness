param(
  [Parameter(Mandatory=$true)][string]$Payload,
  [Parameter(Mandatory=$true)][string]$ExpectedVersion,
  [Parameter(Mandatory=$true)][string]$Version
)
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
[Console]::OutputEncoding = [Text.Encoding]::UTF8
if ($Version -notmatch '^[0-9a-f]{7,40}$') { throw 'Invalid version' }
$AppRoot = [IO.Path]::GetFullPath('D:\ledger\app')
$StaticRoot = [IO.Path]::GetFullPath((Join-Path $AppRoot 'ledger\ledger\static'))
$ReleaseRoot = [IO.Path]::GetFullPath((Join-Path 'D:\ledger\releases' $Version))
if (-not $ReleaseRoot.StartsWith('D:\ledger\releases\',[StringComparison]::OrdinalIgnoreCase)) { throw 'Unsafe release directory' }
function Target-Path([string]$Relative) {
  if ($Relative -eq 'VERSION') { return (Join-Path $AppRoot 'VERSION') }
  $Target = [IO.Path]::GetFullPath((Join-Path $AppRoot $Relative))
  if (-not $Target.StartsWith($StaticRoot+'\',[StringComparison]::OrdinalIgnoreCase)) { throw "Unexpected UI file: $Relative" }
  return $Target
}
function Hash([string]$File) { return (Get-FileHash -LiteralPath $File -Algorithm SHA256).Hash.ToLowerInvariant() }
$VersionFile = Target-Path 'VERSION'
$IndexFile = Target-Path 'ledger/ledger/static/index.html'
if ((Get-Content -LiteralPath $VersionFile -Raw).Trim() -ne $ExpectedVersion) { throw 'Production version changed' }
Add-Type -AssemblyName System.IO.Compression.FileSystem
$Lock = [IO.File]::Open('D:\ledger\releases\ui-release.lock',[IO.FileMode]::OpenOrCreate,[IO.FileAccess]::ReadWrite,[IO.FileShare]::None)
try {
  $Zip = [IO.Compression.ZipFile]::OpenRead($Payload)
  try { foreach ($Entry in $Zip.Entries) { if (-not $Entry.FullName.EndsWith('/') -and $Entry.FullName -ne 'manifest.json') { $null = Target-Path $Entry.FullName } } }
  finally { $Zip.Dispose() }
  New-Item -ItemType Directory -Force -Path $ReleaseRoot | Out-Null
  $Stage = Join-Path $ReleaseRoot 'payload'
  if (Test-Path -LiteralPath $Stage) { throw 'Release stage already exists; inspect before retrying' }
  [IO.Compression.ZipFile]::ExtractToDirectory($Payload,$Stage)
  $Manifest = Get-Content -LiteralPath (Join-Path $Stage 'manifest.json') -Raw -Encoding UTF8 | ConvertFrom-Json
  foreach ($File in $Manifest.files) {
    $Target = Target-Path $File.path
    $Source = [IO.Path]::GetFullPath((Join-Path $Stage $File.path))
    if (-not $Source.StartsWith($Stage+'\',[StringComparison]::OrdinalIgnoreCase)) { throw 'Unsafe manifest source' }
    if ((Hash $Source) -ne $File.sha256) { throw "Payload mismatch: $($File.path)" }
    if ($File.before_sha256) {
      if (-not (Test-Path -LiteralPath $Target) -or (Hash $Target) -ne $File.before_sha256) { throw "Production file changed: $($File.path)" }
    } elseif ((Test-Path -LiteralPath $Target) -and (Hash $Target) -ne $File.sha256) { throw "Unexpected existing file: $($File.path)" }
  }
  $Index = $Manifest.files | Where-Object { $_.path -eq 'ledger/ledger/static/index.html' }
  $Stamp = $Manifest.files | Where-Object { $_.path -eq 'VERSION' }
  if (-not $Index -or -not $Stamp) { throw 'Payload must contain index and version' }
  $Backup = Join-Path $ReleaseRoot 'before.zip'
  $Archive = [IO.Compression.ZipFile]::Open($Backup,[IO.Compression.ZipArchiveMode]::Create)
  try {
    foreach ($File in $Manifest.files) {
      $Target = Target-Path $File.path
      if (Test-Path -LiteralPath $Target) { [IO.Compression.ZipFileExtensions]::CreateEntryFromFile($Archive,$Target,$File.path) | Out-Null }
    }
  } finally { $Archive.Dispose() }
  foreach ($File in $Manifest.files) {
    if ($File.path -in @('VERSION','ledger/ledger/static/index.html')) { continue }
    $Target = Target-Path $File.path
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $Target) | Out-Null
    Copy-Item -LiteralPath (Join-Path $Stage $File.path) -Destination $Target -Force
    if ((Hash $Target) -ne $File.sha256) { throw "Copied asset mismatch: $($File.path)" }
  }
  # The old HTML and its hashed assets remain usable throughout publication.
  if ((Hash $IndexFile) -ne $Index.before_sha256 -or (Hash $VersionFile) -ne $Stamp.before_sha256) { throw 'Entry files changed during staging' }
  $NewIndex = Join-Path $StaticRoot ('index.pending-'+$Version)
  $NewStamp = Join-Path $AppRoot ('VERSION.pending-'+$Version)
  Copy-Item -LiteralPath (Join-Path $Stage $Index.path) -Destination $NewIndex
  Copy-Item -LiteralPath (Join-Path $Stage 'VERSION') -Destination $NewStamp
  $Switched = $false
  try {
    [IO.File]::Replace($NewIndex,$IndexFile,(Join-Path $ReleaseRoot 'index.previous.html')); $Switched = $true
    [IO.File]::Replace($NewStamp,$VersionFile,(Join-Path $ReleaseRoot 'VERSION.previous'))
    $HomeResponse = Invoke-WebRequest -Uri 'http://127.0.0.1:8000/' -UseBasicParsing -TimeoutSec 20
    $Info = Invoke-RestMethod -Uri 'http://127.0.0.1:8000/api/version' -TimeoutSec 20
    if ($HomeResponse.StatusCode -ne 200 -or $Info.version -ne $Version) { throw 'UI publication readback failed' }
    Write-Output ("DEPLOYED_UI " + $Version + " BACKUP " + $Backup + " SERVICE_UNINTERRUPTED")
  } catch {
    if ($Switched) {
      $Restore = Join-Path $ReleaseRoot 'restore'
      [IO.Compression.ZipFile]::ExtractToDirectory($Backup,$Restore)
      Copy-Item -LiteralPath (Join-Path $Restore $Index.path) -Destination $IndexFile -Force
      Copy-Item -LiteralPath (Join-Path $Restore 'VERSION') -Destination $VersionFile -Force
    }
    throw
  }
} finally { $Lock.Dispose() }
