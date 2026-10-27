param([string]$Payload,[string]$ExpectedVersion,[string]$Version)
$ErrorActionPreference = 'Stop'
throw 'Legacy UI-only release is disabled. UI and API must use the same accepted receipt via tools/deploy/ship.sh and deploy-commission.ps1.'
