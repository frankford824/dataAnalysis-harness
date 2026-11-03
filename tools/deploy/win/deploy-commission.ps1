param(
  [Parameter(Mandatory=$true)][string]$Payload,
  [Parameter(Mandatory=$true)][string]$ExpectedVersion,
  [Parameter(Mandatory=$true)][string]$Version,
  [Parameter(Mandatory=$true)][string]$UserBaseUrl,
  [Parameter(Mandatory=$true)][string]$ReadbackPeriod,
  [Parameter(Mandatory=$true)][ValidatePattern('^[0-9a-f]{64}$')][string]$PayloadSha256,
  [string]$BackupManifest = $env:LEDGER_BACKUP_MANIFEST,
  [switch]$AllowTemplateUpdate,
  [switch]$AllowSourceUpdate,
  [switch]$AllowMetricUpdate,
  [switch]$AllowDictionaryUpdate
)
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [Text.Encoding]::UTF8
if ($Version -notmatch '^[0-9a-f]{40}$') { throw 'Release requires a full accepted commit SHA' }
if (-not $BackupManifest -or -not (Test-Path -LiteralPath $BackupManifest)) { throw 'Release requires an existing verified financial backup manifest' }
# A Windows venv launcher owns a separate base-interpreter child. Verify both
# under the actual maintenance identity before stopping any current writer.
$ServicePython = [IO.Path]::GetFullPath('D:\ledger\venv\Scripts\python.exe')
$RuntimeProbeText = & $ServicePython -I -B -c 'import sys,json;print(json.dumps(dict(executable=sys.executable,base_executable=sys._base_executable,version=list(sys.version_info[:3]))))'
if ($LASTEXITCODE -ne 0) { throw 'Configured service interpreter cannot start under this deployment identity' }
$RuntimeProbe = $RuntimeProbeText | ConvertFrom-Json
if ($RuntimeProbe.version[0] -ne 3 -or $RuntimeProbe.version[1] -ne 12 -or
    [IO.Path]::GetFullPath($RuntimeProbe.executable) -ine $ServicePython -or -not $RuntimeProbe.base_executable) {
  throw 'Service runtime differs from the tested Python 3.12 environment'
}
$LedgerRuntimeExecutables = @($ServicePython,[IO.Path]::GetFullPath($RuntimeProbe.base_executable)) | Select-Object -Unique
if ((Get-FileHash -LiteralPath $Payload -Algorithm SHA256).Hash.ToLower() -ne $PayloadSha256) { throw 'Transported payload differs from trusted package SHA' }
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
    if ($Name -eq 'manifest.json' -or $Name -eq 'acceptance-receipt.json') { continue }
    if ($Name -notin @('VERSION','RELEASE-ACCEPTANCE.json','tools/verify-ledger-release.py','tools/bootstrap-ledger-agent.py','tools/ledger-auth.py','tools/ledger-backup.py','tools/ledger_acceptance_inputs.py') -and -not $Name.StartsWith('ledger/ledger/') -and -not $Name.StartsWith('ops/ledger_sentinel/') -and -not $Name.StartsWith('host-agent/finance_agent/') -and -not $Name.StartsWith('agent-worker/ledger_agent/') -and -not $Name.StartsWith('agent-worker/skills/') -and
        $Name -notin @('agent-worker/pyproject.toml','agent-worker/requirements.lock','host-agent/pyproject.toml','host-agent/README.md') -and
        -not ($AllowTemplateUpdate -and $Name -eq 'models/cn-ecommerce/templates.yaml') -and
        -not ($AllowSourceUpdate -and $Name -eq 'models/cn-ecommerce/sources.yaml') -and
        -not ($AllowMetricUpdate -and $Name -eq 'models/cn-ecommerce/metrics.yaml') -and
        -not ($AllowDictionaryUpdate -and $Name -eq 'models/cn-ecommerce/dictionary.csv')) { throw "Unexpected file: $Name" }
    $Destination = [IO.Path]::GetFullPath((Join-Path $AppRoot $Name))
    if (-not $Destination.StartsWith($AppRoot + '\',[StringComparison]::OrdinalIgnoreCase)) { throw "Unsafe destination: $Name" }
  }
} finally { $Zip.Dispose() }
New-Item -ItemType Directory -Force -Path $ReleaseRoot | Out-Null
$Stage = Join-Path $ReleaseRoot 'payload'
if (Test-Path -LiteralPath $Stage) { throw 'Release stage already exists; inspect it before retrying' }
[IO.Compression.ZipFile]::ExtractToDirectory($Payload,$Stage)
$Manifest = Get-Content -LiteralPath (Join-Path $Stage 'manifest.json') -Raw -Encoding UTF8 | ConvertFrom-Json
$ReceiptPath = Join-Path $Stage 'acceptance-receipt.json'
if (-not (Test-Path -LiteralPath $ReceiptPath)) { throw 'Missing business acceptance receipt' }
$Receipt = Get-Content -LiteralPath $ReceiptPath -Raw -Encoding UTF8 | ConvertFrom-Json
if ($Receipt.receipt_sha -ne $Manifest.acceptance_receipt_sha -or $Manifest.version -ne $Version) { throw 'Acceptance receipt does not match payload' }
function Assert-ProductionCodeBaseline {
  if (-not $Manifest.production_base_files -or -not $Manifest.production_base_files.Count) { throw 'Actual production source baseline is required' }
  $ExpectedSourcePaths = @($Manifest.production_base_files | Where-Object { $_.path.EndsWith('.py') } | ForEach-Object { $_.path } | Sort-Object)
  $ActualSourcePaths = @(Get-ChildItem -LiteralPath (Join-Path $AppRoot 'ledger\ledger') -File -Recurse -Filter '*.py' | ForEach-Object {
    $_.FullName.Substring($AppRoot.Length+1).Replace('\','/')
  } | Sort-Object)
  if (Compare-Object -ReferenceObject $ExpectedSourcePaths -DifferenceObject $ActualSourcePaths) { throw 'Actual production Python source inventory changed' }
  foreach ($BaselineFile in $Manifest.production_base_files) {
    if (-not $BaselineFile.path.StartsWith('ledger/ledger/') -or $BaselineFile.path.Contains('\') -or
        $BaselineFile.path.Split('/') -contains '..' -or $BaselineFile.sha256 -notmatch '^[0-9a-f]{64}$') {
      throw 'Invalid production source baseline entry'
    }
    $BaselineTarget = [IO.Path]::GetFullPath((Join-Path $AppRoot $BaselineFile.path))
    if (-not $BaselineTarget.StartsWith($AppRoot+'\',[StringComparison]::OrdinalIgnoreCase) -or
        -not (Test-Path -LiteralPath $BaselineTarget) -or
        (Get-FileHash -LiteralPath $BaselineTarget -Algorithm SHA256).Hash.ToLower() -ne $BaselineFile.sha256) {
      throw "Production source baseline changed: $($BaselineFile.path)"
    }
  }
}
Assert-ProductionCodeBaseline
foreach ($File in $Manifest.files) {
  $Source = [IO.Path]::GetFullPath((Join-Path $Stage $File.path))
  $Target = [IO.Path]::GetFullPath((Join-Path $AppRoot $File.path))
  if (-not $Source.StartsWith($Stage+'\',[StringComparison]::OrdinalIgnoreCase) -or
      -not $Target.StartsWith($AppRoot+'\',[StringComparison]::OrdinalIgnoreCase)) { throw 'Manifest path outside workspace' }
  if ((Get-FileHash -LiteralPath $Source -Algorithm SHA256).Hash.ToLower() -ne $File.sha256) { throw "Payload hash mismatch: $($File.path)" }
  if ($File.is_new -and $File.path.EndsWith('.py') -and (Test-Path -LiteralPath $Target)) { throw "New module already exists on production: $($File.path)" }
  if ($File.before_sha256 -and (Test-Path -LiteralPath $Target)) {
    # A production hot fix may already be exactly the accepted release content.
    $Current = (Get-FileHash -LiteralPath $Target -Algorithm SHA256).Hash.ToLower()
    if ($Current -ne $File.before_sha256 -and $Current -ne $File.sha256) { throw "Production file changed: $($File.path)" }
  }
}
& 'D:\ledger\venv\Scripts\python.exe' (Join-Path $Stage 'ledger\ledger\release_gate.py') --receipt $ReceiptPath --version $Manifest.version --model-sha $Manifest.model_sha
if ($LASTEXITCODE -ne 0) { throw 'Business acceptance receipt rejected' }
& 'D:\ledger\venv\Scripts\python.exe' (Join-Path $Stage 'tools\ledger-backup.py') verify --manifest $BackupManifest --version $ExpectedVersion --compare-root 'D:\ledger\home'
if ($LASTEXITCODE -ne 0) { throw 'Financial backup/restore verification failed' }
$ReleaseMarker = Join-Path $AppRoot '.release-in-progress.json'
$MarkerStream = [IO.File]::Open($ReleaseMarker,[IO.FileMode]::CreateNew,[IO.FileAccess]::Write,[IO.FileShare]::None)
try {
  $MarkerBytes = [Text.Encoding]::UTF8.GetBytes((@{version=$Version;acquired_at=[DateTime]::UtcNow.ToString('o')} | ConvertTo-Json -Compress))
  $MarkerStream.Write($MarkerBytes,0,$MarkerBytes.Length)
} finally { $MarkerStream.Dispose() }
try {
$CutoverSafeToResume = $false
$LockedVersion = (Get-Content -LiteralPath (Join-Path $AppRoot 'VERSION') -Raw).Trim()
if ($LockedVersion -ne $ExpectedVersion) {
  $CutoverSafeToResume = $true
  throw "Production version changed before release lock: $LockedVersion"
}
$Backup = Join-Path $ReleaseRoot 'before.zip'
$BeforeExistence = @{}
$Archive = [IO.Compression.ZipFile]::Open($Backup,[IO.Compression.ZipArchiveMode]::Create)
try {
  # Capture preconditions under the release marker; recheck after writer exit.
  foreach ($File in $Manifest.files) {
    $Target = Join-Path $AppRoot $File.path
    $BeforeExistence[$File.path] = [bool](Test-Path -LiteralPath $Target)
    if ($File.before_sha256 -and -not ($File.allow_absent -and -not $BeforeExistence[$File.path])) {
      if (-not (Test-Path -LiteralPath $Target) -or
          ((Get-FileHash -LiteralPath $Target -Algorithm SHA256).Hash.ToLower() -notin @($File.before_sha256,$File.sha256))) {
        throw "Production file changed during staging: $($File.path)"
      }
    }
  }
  foreach ($File in $Manifest.files) {
    $Target = Join-Path $AppRoot $File.path
    if (Test-Path -LiteralPath $Target) { [IO.Compression.ZipFileExtensions]::CreateEntryFromFile($Archive,$Target,$File.path) | Out-Null }
  }
} finally { $Archive.Dispose() }

function Get-LedgerApiWriterCandidates {
  param([object[]]$Processes)
  $ExpectedPython = [IO.Path]::GetFullPath('D:\ledger\venv\Scripts\python.exe')
  $AllowedPython = @($ExpectedPython)+@($LedgerRuntimeExecutables)
  return @($Processes | Where-Object {
    $Item = $_
    $Item.CommandLine -and $Item.CommandLine -match '(^|\s)"?ledger\.api:app"?(\s|$)' -and
      @($AllowedPython | Where-Object { $_ -and ($Item.ExecutablePath -ieq $_ -or
        $Item.CommandLine.TrimStart().StartsWith('"'+$_+'"',[StringComparison]::OrdinalIgnoreCase) -or
        $Item.CommandLine.TrimStart().StartsWith($_+' ',[StringComparison]::OrdinalIgnoreCase)) }).Count
  })
}
function Stop-LedgerOnly {
  $ExpectedSupervisor = 'D:\ledger\bin\serve.ps1'
  $Task = Get-ScheduledTask -TaskName 'LedgerHarness'
  if (-not @($Task.Actions | Where-Object { $_.Arguments -and $_.Arguments.Contains($ExpectedSupervisor) }).Count) {
    throw 'LedgerHarness does not belong to the expected ledger supervisor'
  }
  # Capture ancestry before Stop-ScheduledTask can orphan its children.
  $Processes = @(Get-CimInstance Win32_Process)
  $OwnedParents = [Collections.Generic.HashSet[uint32]]::new()
  foreach ($Item in $Processes) {
    if ($Item.CommandLine -and $Item.CommandLine.Contains($ExpectedSupervisor)) { $null = $OwnedParents.Add([uint32]$Item.ProcessId) }
  }
  for ($Depth=0; $Depth -lt 20; $Depth++) {
    $Added = $false
    foreach ($Item in $Processes) {
      if ($OwnedParents.Contains([uint32]$Item.ParentProcessId) -and $OwnedParents.Add([uint32]$Item.ProcessId)) { $Added = $true }
    }
    if (-not $Added) { break }
  }
  Stop-ScheduledTask -TaskName 'LedgerHarness'
  for ($Attempt=0; $Attempt -lt 120; $Attempt++) {
    $Processes = @(Get-CimInstance Win32_Process)
    $Writers = @(Get-LedgerApiWriterCandidates -Processes $Processes)
    $OwnedWriters = @()
    foreach ($Item in $Writers) {
      if ($OwnedParents.Contains([uint32]$Item.ProcessId) -or $OwnedParents.Contains([uint32]$Item.ParentProcessId) -or
          ($Item.CommandLine -match '--app-dir' -and $Item.CommandLine.Contains((Join-Path $AppRoot 'ledger')))) {
        $null = $OwnedParents.Add([uint32]$Item.ProcessId)
        $OwnedWriters += $Item
      }
    }
    if ($Writers.Count -ne $OwnedWriters.Count) { throw 'Unowned ledger writer exists; cannot prove a static financial snapshot' }
    $Listeners = @(Get-NetTCPConnection -State Listen -LocalPort 8000 -ErrorAction SilentlyContinue)
    foreach ($Listener in $Listeners) {
      if (-not $OwnedParents.Contains([uint32]$Listener.OwningProcess) -and -not @($OwnedWriters | Where-Object { $_.ProcessId -eq $Listener.OwningProcess }).Count) {
        throw 'Port 8000 owner is not proved to belong to this ledger process tree'
      }
    }
    foreach ($Item in $OwnedWriters) {
      $Live = Get-CimInstance Win32_Process -Filter ("ProcessId="+$Item.ProcessId) -ErrorAction SilentlyContinue
      if ($Live -and $Live.CreationDate -eq $Item.CreationDate) { Stop-Process -Id $Item.ProcessId -Force -ErrorAction Stop }
    }
    if (-not $OwnedWriters.Count -and -not $Listeners.Count -and (Get-ScheduledTask -TaskName 'LedgerHarness').State -ne 'Running') { return }
    Start-Sleep -Milliseconds 250
  }
  throw 'Ledger writers did not exit; no static financial snapshot may be captured'
}
function Start-AndCheck {
  param([string]$WantedVersion = $Version)
  Start-ScheduledTask -TaskName 'LedgerHarness'
  for ($Attempt=0; $Attempt -lt 60; $Attempt++) {
    try {
      $Response = Invoke-WebRequest -Uri 'http://127.0.0.1:8000/api/version' -TimeoutSec 5 -UseBasicParsing
      if ($Response.StatusCode -eq 200 -and ($Response.Content | ConvertFrom-Json).version -eq $WantedVersion) { return }
    } catch { }
    Start-Sleep -Milliseconds 500
  }
  throw 'New commission API did not become ready'
}
$HistoryReceipt = Join-Path $ReleaseRoot 'history-before.json'
try {
  Stop-LedgerOnly
} catch {
  $StopFailure = $_
  # A closed socket is not proof that an in-flight writer has exited.
  $RemainingWriters = @(Get-LedgerApiWriterCandidates -Processes @(Get-CimInstance Win32_Process))
  if (-not $RemainingWriters.Count -and -not (Get-NetTCPConnection -State Listen -LocalPort 8000 -ErrorAction SilentlyContinue)) {
    Start-AndCheck -WantedVersion $ExpectedVersion
    $CutoverSafeToResume = $true
  }
  throw $StopFailure
}
$StoppedVersion = (Get-Content -LiteralPath (Join-Path $AppRoot 'VERSION') -Raw).Trim()
if ($StoppedVersion -ne $ExpectedVersion) { throw 'Production version changed before stopped snapshot' }
try {
  & 'D:\ledger\venv\Scripts\python.exe' (Join-Path $Stage 'tools\verify-ledger-release.py') capture 'D:\ledger\home' $HistoryReceipt --offline-version $StoppedVersion
  if ($LASTEXITCODE -ne 0) { throw 'Could not capture stopped frozen financial history before release' }
} catch {
  Start-AndCheck -WantedVersion $ExpectedVersion
  $CutoverSafeToResume = $true
  throw
}
$ModelHashes = @{}
$FilesReplaced = $false
Get-ChildItem -LiteralPath (Join-Path $AppRoot 'models\cn-ecommerce') -File | ForEach-Object {
  $ModelHashes[$_.Name] = (Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash
}
try {
  # Stop-LedgerOnly completed above. Recheck all code/model preconditions now.
  & 'D:\ledger\venv\Scripts\python.exe' (Join-Path $Stage 'tools\ledger-backup.py') verify --manifest $BackupManifest --version $ExpectedVersion --compare-root 'D:\ledger\home'
  if ($LASTEXITCODE -ne 0) { throw 'Stopped financial history is newer than, or differs from, the verified backup' }
  Assert-ProductionCodeBaseline
  foreach ($File in $Manifest.files) {
    $Target = Join-Path $AppRoot $File.path
    $ExistsNow = [bool](Test-Path -LiteralPath $Target)
    if ($ExistsNow -ne $BeforeExistence[$File.path]) { throw "Production file existence changed before cutover: $($File.path)" }
    if ($File.before_sha256 -and -not ($File.allow_absent -and -not $ExistsNow) -and ((-not $ExistsNow) -or
        ((Get-FileHash -LiteralPath $Target -Algorithm SHA256).Hash.ToLower() -notin @($File.before_sha256,$File.sha256)))) {
      throw "Production changed before cutover: $($File.path)"
    }
  }
  $FilesReplaced = $true
  foreach ($File in $Manifest.files) {
    $Target = Join-Path $AppRoot $File.path
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $Target) | Out-Null
    Copy-Item -LiteralPath (Join-Path $Stage $File.path) -Destination $Target -Force
  }
  foreach ($Name in $ModelHashes.Keys) {
    $ExpectedHash = $ModelHashes[$Name]
    $TemplatePayload = @($Manifest.files | Where-Object { $_.path -eq 'models/cn-ecommerce/templates.yaml' })
    if ($AllowTemplateUpdate -and $Name -eq 'templates.yaml' -and $TemplatePayload.Count -eq 1) { $ExpectedHash = $TemplatePayload[0].sha256 }
    $SourcePayload = @($Manifest.files | Where-Object { $_.path -eq 'models/cn-ecommerce/sources.yaml' })
    if ($AllowSourceUpdate -and $Name -eq 'sources.yaml' -and $SourcePayload.Count -eq 1) { $ExpectedHash = $SourcePayload[0].sha256 }
    $MetricPayload = @($Manifest.files | Where-Object { $_.path -eq 'models/cn-ecommerce/metrics.yaml' })
    if ($AllowMetricUpdate -and $Name -eq 'metrics.yaml' -and $MetricPayload.Count -eq 1) { $ExpectedHash = $MetricPayload[0].sha256 }
    $DictionaryPayload = @($Manifest.files | Where-Object { $_.path -eq 'models/cn-ecommerce/dictionary.csv' })
    if ($AllowDictionaryUpdate -and $Name -eq 'dictionary.csv' -and $DictionaryPayload.Count -eq 1) { $ExpectedHash = $DictionaryPayload[0].sha256 }
    if ((Get-FileHash -LiteralPath (Join-Path $AppRoot ('models\cn-ecommerce\'+$Name)) -Algorithm SHA256).Hash -ne $ExpectedHash) { throw "Model changed during code release: $Name" }
  }
  # Warm immutable read projections while background writers are stopped.
  # This does not recompute, settle, or alter business rules/amounts.
  if (Test-Path -LiteralPath (Join-Path $AppRoot 'ledger\ledger\warm_reads.py')) {
    $env:PYTHONPATH = Join-Path $AppRoot 'ledger'
    & 'D:\ledger\venv\Scripts\python.exe' -m ledger.warm_reads --root 'D:\ledger\home' --model (Join-Path $AppRoot 'models\cn-ecommerce')
    if ($LASTEXITCODE -ne 0) { throw 'Read projection warm-up failed' }
  }
  Start-AndCheck
  & 'D:\ledger\venv\Scripts\python.exe' (Join-Path $Stage 'ledger\ledger\release_gate.py') --receipt $ReceiptPath --version $Version --model-root (Join-Path $AppRoot 'models\cn-ecommerce')
  if ($LASTEXITCODE -ne 0) { throw 'Deployed model differs from acceptance receipt' }
  & 'D:\ledger\venv\Scripts\python.exe' (Join-Path $Stage 'tools\verify-ledger-release.py') verify 'D:\ledger\home' $HistoryReceipt --acceptance $ReceiptPath --readback-url $UserBaseUrl --period $ReadbackPeriod
  if ($LASTEXITCODE -ne 0) { throw 'Post-release financial history/readback check failed' }
  Copy-Item -LiteralPath $ReceiptPath -Destination (Join-Path $ReleaseRoot 'acceptance-receipt.json')
  $CutoverSafeToResume = $true
  Write-Output ("DEPLOYED " + $Version + " BACKUP " + $Backup)
} catch {
  $Failure = $_
  Stop-LedgerOnly
  if (-not $FilesReplaced) {
    & 'D:\ledger\venv\Scripts\python.exe' (Join-Path $Stage 'tools\verify-ledger-release.py') verify-history 'D:\ledger\home' $HistoryReceipt --expected-version $ExpectedVersion --offline-version $ExpectedVersion
    if ($LASTEXITCODE -ne 0) { throw 'Frozen history differs before rollback restart; maintenance remains active' }
    Start-AndCheck -WantedVersion $ExpectedVersion
    $CutoverSafeToResume = $true
    throw $Failure
  }
  $Restore = Join-Path $ReleaseRoot 'restore'
  [IO.Compression.ZipFile]::ExtractToDirectory($Backup,$Restore)
  foreach ($File in $Manifest.files) {
    $OldFile = Join-Path $Restore $File.path
    $RollbackTarget = [IO.Path]::GetFullPath((Join-Path $AppRoot $File.path))
    if (-not $RollbackTarget.StartsWith($AppRoot+'\',[StringComparison]::OrdinalIgnoreCase)) { throw 'Unsafe rollback target' }
    if (Test-Path -LiteralPath $OldFile) { Copy-Item -LiteralPath $OldFile -Destination $RollbackTarget -Force }
    elseif (($File.is_new -or $File.allow_absent) -and (Test-Path -LiteralPath $RollbackTarget)) { Remove-Item -LiteralPath $RollbackTarget -Force }
  }
  $RollbackVersion = (Get-Content -LiteralPath (Join-Path $AppRoot 'VERSION') -Raw).Trim()
  if ($RollbackVersion -ne $ExpectedVersion) { throw 'Rollback version identity failed; service remains stopped' }
  Assert-ProductionCodeBaseline
  & 'D:\ledger\venv\Scripts\python.exe' (Join-Path $Stage 'tools\verify-ledger-release.py') verify-history 'D:\ledger\home' $HistoryReceipt --expected-version $ExpectedVersion --offline-version $RollbackVersion
  if ($LASTEXITCODE -ne 0) { throw 'Financial history still differs after code rollback; maintenance remains active' }
  Start-AndCheck -WantedVersion $ExpectedVersion
  $CutoverSafeToResume = $true
  throw $Failure
}
} finally {
  # Clear only this release's marker. A failed process leaves maintenance visible.
  if ($CutoverSafeToResume -and (Test-Path -LiteralPath $ReleaseMarker)) {
    $HeldMarker = Get-Content -LiteralPath $ReleaseMarker -Raw | ConvertFrom-Json
    if ($HeldMarker.version -eq $Version) { Remove-Item -LiteralPath $ReleaseMarker -Force }
  }
}
