[CmdletBinding(SupportsShouldProcess=$true)]
param([Parameter(Mandatory=$true)][string]$ConfigPath)
$ErrorActionPreference = 'Stop'
$taskName = 'KeywordLibrary-Ziniao-Agent'
$markerText = 'keyword-library-ziniao-agent:v1'
$module = Join-Path $PSScriptRoot 'KeywordAgent.psm1'
Import-Module $module -Force
$config = Import-PowerShellDataFile -LiteralPath $ConfigPath
Assert-KeywordAgentConfig $config
Assert-KeywordAgentPort $config | Out-Null
$target = Join-Path $env:LOCALAPPDATA 'KeywordLibraryAgent'
$marker = Join-Path $target 'marker.json'
$existing = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
if (Test-Path -LiteralPath $target) {
  if (-not (Test-Path -LiteralPath $marker)) { throw "配置目录已存在但不属于本项目：$target" }
  $owned = Get-Content -Raw -LiteralPath $marker | ConvertFrom-Json
  if ($owned.marker -ne $markerText -or $owned.projectRoot -ne $config.ProjectRoot) { throw '现有配置目录 marker 不匹配' }
}
if ($existing -and $existing.Description -ne $markerText) { throw "计划任务已存在且归属未知：$taskName" }

if ($PSCmdlet.ShouldProcess($target, '安装紫鸟采集 Agent')) {
  New-Item -ItemType Directory -Path $target -Force | Out-Null
  Copy-Item -LiteralPath $ConfigPath -Destination (Join-Path $target 'config.psd1') -Force
  Copy-Item -LiteralPath $config.PrivateKey -Destination (Join-Path $target 'id_ed25519') -Force
  Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'run-supervisor.ps1') -Destination (Join-Path $target 'run-supervisor.ps1') -Force
  Copy-Item -LiteralPath $module -Destination (Join-Path $target 'KeywordAgent.psm1') -Force
  Set-Content -LiteralPath (Join-Path $target 'known_hosts') -Value $config.ServerHostKeyLine -Encoding ascii
  $actualHostKey = (& ssh-keygen.exe -lf (Join-Path $target 'known_hosts') -E sha256).Split(' ', [System.StringSplitOptions]::RemoveEmptyEntries)[1]
  if ($actualHostKey -ne $config.ServerSshHostKey) { throw '服务器 SSH 主机指纹与 known_hosts 不匹配' }
  @{ marker=$markerText; projectRoot=$config.ProjectRoot } | ConvertTo-Json | Set-Content -LiteralPath $marker -Encoding utf8
  & icacls.exe $target /inheritance:r /grant:r "$env:USERNAME`:(OI)(CI)F" 'SYSTEM:(OI)(CI)F' | Out-Null
  if ($LASTEXITCODE -ne 0) { throw '无法限制 Agent 配置目录 ACL' }
  foreach ($secret in @((Join-Path $target 'config.psd1'),(Join-Path $target 'id_ed25519'))) { Assert-KeywordAgentAcl $secret }
  $actionSpec = Get-KeywordAgentTaskActionSpec -Target $target -ProjectRoot $config.ProjectRoot
  $action = New-ScheduledTaskAction -Execute $actionSpec.Execute -Argument $actionSpec.Argument -WorkingDirectory $actionSpec.WorkingDirectory
  $trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
  $settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)
  Register-ScheduledTask -TaskName $taskName -Description $markerText -Action $action -Trigger $trigger -Settings $settings -RunLevel Limited -Force | Out-Null
}
