$ErrorActionPreference='Stop'
$ProgressPreference='SilentlyContinue'
[Console]::OutputEncoding=[Text.Encoding]::UTF8
$base=[IO.Path]::GetFullPath('D:\ledger')
$archive='\\192.168.0.125\dataAnalysis\台账系统\90_历史版本\系统核算留档\bundles\ledger'
& D:\ledger\venv\Scripts\python.exe C:\Windows\Temp\verify_nas_bundles.py
if($LASTEXITCODE -ne 0){throw 'Source bundle inventory changed'}
$manifest=Get-Content C:\ledger-storage-migration\20260913\nas-bundles.json -Raw -Encoding UTF8 | ConvertFrom-Json
$done=@()
foreach($bundle in $manifest.bundles){
 $source=[IO.Path]::GetFullPath($bundle.source)
 $target=[IO.Path]::GetFullPath($bundle.target)
 $stage=[IO.Path]::GetFullPath($source+'.local-migration')
 $allowed=$source.StartsWith($base+'\qa\',[StringComparison]::OrdinalIgnoreCase) -or $source.StartsWith($base+'\releases\',[StringComparison]::OrdinalIgnoreCase) -or ($source -in @('D:\ledger\backups','D:\ledger\work-probe','D:\ledger\commission-deploy','D:\ledger\workspace-migration.db'))
 if(-not $allowed -or -not $stage.StartsWith($base+'\',[StringComparison]::OrdinalIgnoreCase) -or -not $target.StartsWith($archive+'\',[StringComparison]::OrdinalIgnoreCase)){throw 'Bundle path boundary'}
 $alreadyLinked=((Get-Item -LiteralPath $source -Force).LinkType -eq 'SymbolicLink')
 if(-not $alreadyLinked -and (Test-Path -LiteralPath $stage)){throw 'Existing migration stage'}
 if(-not (Test-Path -LiteralPath $target)){throw 'Verified destination missing'}
 if(-not $alreadyLinked -and -not $bundle.is_file){
  $links=@(Get-ChildItem -LiteralPath $source -Directory -Recurse -Force | Where-Object {$_.Attributes -band [IO.FileAttributes]::ReparsePoint})
  if($links.Count){throw 'Unexpected directory reparse point in source'}
 }
 if(-not $alreadyLinked){Move-Item -LiteralPath $source -Destination $stage}
 try {
  if(-not $alreadyLinked){New-Item -ItemType SymbolicLink -Path $source -Target $target | Out-Null}
  $item=Get-Item -LiteralPath $source -Force
  & D:\ledger\venv\Scripts\python.exe C:\Windows\Temp\verify_bundle_link.py $source $target
  if($LASTEXITCODE -ne 0){throw 'Link target mismatch'}
  foreach($sample in @($bundle.files[0],$bundle.files[-1])){
   $path=if($bundle.is_file){$source}else{Join-Path $source $sample.relative}
   if((Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash.ToLower() -ne $sample.sha256){throw 'Link readback mismatch'}
  }
 } catch {
  if((Test-Path -LiteralPath $stage) -and (Get-Item -LiteralPath $source -Force -ErrorAction SilentlyContinue).LinkType -eq 'SymbolicLink'){if($bundle.is_file){Remove-Item -LiteralPath $source -Force}else{[IO.Directory]::Delete($source,$false)}}
  if(-not (Test-Path -LiteralPath $source)){Move-Item -LiteralPath $stage -Destination $source}
  throw
 }
 # Stage is an exact, previously verified child of D:\ledger, never a computed external tree.
 if([IO.Path]::GetFullPath($stage) -ne ($source+'.local-migration')){throw 'Stage boundary changed'}
 if(Test-Path -LiteralPath $stage){if($bundle.is_file){Remove-Item -LiteralPath $stage -Force}else{Remove-Item -LiteralPath $stage -Recurse -Force}}
 $done+=@{source=$source;target=$target;files=$bundle.files.Count}
 $done | ConvertTo-Json -Depth 4 | Set-Content C:\ledger-storage-migration\20260913\nas-bundle-cutover.json -Encoding UTF8
 Write-Output ('BUNDLE_MOVED '+$source)
}
Write-Output ('BUNDLES_DONE count='+$done.Count+' free_d='+(Get-PSDrive D).Free)

