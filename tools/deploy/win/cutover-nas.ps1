# 一次性 NAS 切换。密码只进入 Windows 凭据对象，不写仓库、配置或日志。

$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [Text.Encoding]::UTF8
$Root = 'D:\ledger'
$Share = '\\192.168.0.125\dataAnalysis'
$Marker = Join-Path $Root 'nas.enabled'

$me = [Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
if (-not $me.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) { throw '请用管理员 PowerShell 运行' }

$mapping = Get-SmbGlobalMapping -ErrorAction SilentlyContinue |
  Where-Object { $_.RemotePath.TrimEnd('\') -ieq $Share.TrimEnd('\') } |
  Select-Object -First 1
if ($mapping) {
  $candidateRoot = $mapping.LocalPath.TrimEnd('\') + '\台账系统'
  if ($mapping.Status.ToString() -ne 'Connected' -or
      -not (Test-Path -LiteralPath $candidateRoot -PathType Container)) {
    Write-Output ('现有全局映射 ' + $mapping.LocalPath + ' 状态为 ' + $mapping.Status + '，将重新建立')
    Remove-SmbGlobalMapping -LocalPath $mapping.LocalPath -Force -ErrorAction Stop
    $mapping = $null
  }
}
if (-not $mapping) {
  Write-Output '请输入 NAS 账号 fwk 的密码。密码不会写入应用配置。'
  $credential = Get-Credential -UserName 'fwk' -Message 'NAS SMB 凭据'
  New-SmbGlobalMapping -LocalPath 'X:' -RemotePath $Share -Credential $credential -Persistent $true | Out-Null
  $mapping = Get-SmbGlobalMapping -LocalPath 'X:' -ErrorAction Stop
}
$NasRoot = $mapping.LocalPath.TrimEnd('\') + '\台账系统'
Write-Output ('使用全局映射 ' + $mapping.LocalPath + ' -> ' + $mapping.RemotePath)
if (-not (Test-Path -LiteralPath $NasRoot -PathType Container)) { throw "映射成功但看不到 $NasRoot" }

$speed = (Get-NetAdapter -Physical | Where-Object Status -eq 'Up' |
  Sort-Object LinkSpeed -Descending | Select-Object -First 1 -ExpandProperty LinkSpeed)
Write-Output ('链路：' + $speed)
if ($speed -notmatch '1 Gbps|2.5 Gbps|10 Gbps') { throw "链路不是千兆或更高：$speed" }

$indexTask = Get-ScheduledTask -TaskName 'LedgerIndexer' -ErrorAction Stop
if ($indexTask.State -ne 'Running') { Start-ScheduledTask -TaskName 'LedgerIndexer' }
for ($i = 1; $i -le 120; $i++) {
  Start-Sleep -Seconds 1
  try {
    $health = Invoke-RestMethod 'http://127.0.0.1:8765/health' -TimeoutSec 3
    if ($health.ok) { break }
  } catch { }
}
if (-not $health.ok) { throw '索引服务两分钟内没有就绪' }

Write-Output '等待首次索引完成。新文件先稳定 60 秒，这是有意的部分复制保护。'
for ($i = 1; $i -le 120; $i++) {
  Start-Sleep -Seconds 30
  $status = Invoke-RestMethod 'http://127.0.0.1:8765/status' -TimeoutSec 10
  $stabilizing = [int]($status.files.stabilizing)
  $errors = [int]($status.files.error) + [int]($status.files.quarantined)
  Write-Output ('  ready=' + [int]($status.files.ready) + ' stabilizing=' + $stabilizing + ' errors=' + $errors)
  if ($status.root_reachable -and -not $status.link_degraded -and $stabilizing -eq 0 -and $errors -eq 0 -and [int]($status.files.ready) -gt 0) { break }
}
if ($stabilizing -ne 0 -or $errors -ne 0 -or -not $status.root_reachable) { throw '首次索引门禁未通过，不切换网页上传' }

Set-Content -LiteralPath $Marker -Value ('enabled ' + (Get-Date -Format o)) -Encoding ASCII
Stop-ScheduledTask -TaskName 'LedgerHarness' -ErrorAction SilentlyContinue
Start-Sleep -Seconds 2
Start-ScheduledTask -TaskName 'LedgerHarness'
Write-Output '已切换到 NAS 自动接收；网页上传将返回 410。'
