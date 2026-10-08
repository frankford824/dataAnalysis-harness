# Update only the existing indexer task, preserving identity/action/security.
$ErrorActionPreference = 'Stop'
$task = Get-ScheduledTask -TaskName 'LedgerIndexer'
$triggers = @($task.Triggers | Where-Object { $_.CimClass.CimClassName -ne 'MSFT_TaskTimeTrigger' })
$triggers += New-ScheduledTaskTrigger -Once -At ((Get-Date).AddMinutes(1)) `
  -RepetitionInterval (New-TimeSpan -Minutes 5) -RepetitionDuration (New-TimeSpan -Days 3650)
$settings = $task.Settings
$settings.ExecutionTimeLimit = 'PT0S'
$settings.MultipleInstances = 2 # IgnoreNew: no overlapping supervisors.
$settings.RestartCount = 999
$settings.RestartInterval = 'PT1M'
$settings.StartWhenAvailable = $true
Set-ScheduledTask -TaskName 'LedgerIndexer' -Trigger $triggers -Settings $settings | Out-Null
Write-Output 'LedgerIndexer: startup plus periodic recovery, IgnoreNew, no runtime limit'
