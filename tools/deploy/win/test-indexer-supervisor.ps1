$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'indexer.ps1') -ValidateOnly
function Assert-Equal($Got,$Want) { if ($Got -cne $Want) { throw "Expected [$Want], got [$Got]" } }
Assert-Equal (Get-IndexerRestartReason $false 2 300) ''
Assert-Equal (Get-IndexerRestartReason $false 3 179) ''
Assert-Equal (Get-IndexerRestartReason $false 3 180) 'startup exceeded 180 seconds without a healthy endpoint'
Assert-Equal (Get-IndexerRestartReason $true 3 20) 'health endpoint failed three consecutive probes'
Assert-Equal (Get-IndexerRestartReason $true 0 300) ''
$stale = [pscustomobject]@{RemotePath='\\192.168.0.125\dataAnalysis';LocalPath='Z:';Status=6}
$healthy = [pscustomobject]@{RemotePath='\\192.168.0.125\dataAnalysis';LocalPath='X:';Status=0}
$other = [pscustomobject]@{RemotePath='\\other\share';LocalPath='Y:';Status=0}
Assert-Equal (Get-IndexerNasRoot @($stale,$healthy,$other)) ('X:\' + (-join ([char[]](0x53f0,0x8d26,0x7cfb,0x7edf))))
Assert-Equal (Get-IndexerNasRoot @($stale,$other)) ('X:\' + (-join ([char[]](0x53f0,0x8d26,0x7cfb,0x7edf))))
Write-Output 'PASS 7 supervisor policy checks'
