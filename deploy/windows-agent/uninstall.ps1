[CmdletBinding(SupportsShouldProcess=$true)]
param()
$ErrorActionPreference='Stop'
$taskName='KeywordLibrary-Ziniao-Agent'
$markerText='keyword-library-ziniao-agent:v1'
$target=Join-Path $env:LOCALAPPDATA 'KeywordLibraryAgent'
$marker=Join-Path $target 'marker.json'
if (-not (Test-Path -LiteralPath $marker)) { throw '未找到关键词库 Agent marker，拒绝删除未知资源' }
$owned=Get-Content -Raw -LiteralPath $marker | ConvertFrom-Json
if ($owned.marker -ne $markerText) { throw 'Agent marker 不匹配，拒绝卸载' }
$task=Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
if ($task -and $task.Description -ne $markerText) { throw '同名计划任务不属于关键词库，拒绝卸载' }
if ($PSCmdlet.ShouldProcess($taskName,'停止并删除关键词库专用计划任务')) {
  if ($task) {
    Stop-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
  }
  $pidFile=Join-Path $target 'agent.pid'
  if (Test-Path -LiteralPath $pidFile) {
    $ownedPid=[int](Get-Content -Raw -LiteralPath $pidFile)
    $process=Get-CimInstance Win32_Process -Filter "ProcessId=$ownedPid" -ErrorAction SilentlyContinue
    if ($process -and $process.CommandLine -match '(?i)(^|\s)-m\s+agent\.run(\s|$)') {
      Stop-Process -Id $ownedPid -Force -ErrorAction SilentlyContinue
    }
  }
  Remove-Item -LiteralPath $target -Recurse -Force
}
