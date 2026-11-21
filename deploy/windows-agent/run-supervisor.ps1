param([Parameter(Mandatory=$true)][string]$ConfigPath)
$ErrorActionPreference = 'Stop'
$Config = Import-PowerShellDataFile -LiteralPath $ConfigPath
Import-Module (Join-Path $PSScriptRoot 'KeywordAgent.psm1') -Force
Assert-KeywordAgentConfig $Config -InstalledRoot $PSScriptRoot
$mutex = [System.Threading.Mutex]::new($false, 'Local\KeywordLibraryZiniaoAgent')
if (-not $mutex.WaitOne(0)) { throw '关键词库紫鸟 Agent 已由另一个监督器运行' }
$agentProcess = $null
$sshProcess = $null
try {
  $knownHosts = Join-Path $PSScriptRoot 'known_hosts'
  $privateKey = Join-Path $PSScriptRoot 'id_ed25519'
  $agentMarker = Join-Path $PSScriptRoot 'marker.json'
  $actualHostKey = (& ssh-keygen.exe -lf $knownHosts -E sha256).Split(' ', [System.StringSplitOptions]::RemoveEmptyEntries)[1]
  if ($actualHostKey -ne $Config.ServerSshHostKey) { throw '服务器 SSH 主机指纹不匹配' }
  $env:KEYWORD_AGENT_HOST='127.0.0.1'
  $env:KEYWORD_AGENT_PORT='7100'
  $env:KEYWORD_BROWSER_AGENT_TOKEN=$Config.AgentToken
  $env:KEYWORD_ZINIAO_STORE_ID=$Config.StoreId
  $env:KEYWORD_AGENT_DATA_DIR=(Join-Path $PSScriptRoot 'data')

  function Test-AgentHealth {
    try { Invoke-RestMethod 'http://127.0.0.1:7100/v1/health' -TimeoutSec 2 | Out-Null; return $true }
    catch { return $false }
  }

  function Stop-OwnedAgent {
    if ($script:agentProcess -and -not $script:agentProcess.HasExited) {
      Stop-Process -Id $script:agentProcess.Id -Force -ErrorAction SilentlyContinue
    }
    $script:agentProcess = $null
  }

  function Start-OwnedAgent {
    $owned = Assert-KeywordAgentPort $Config
    if ($owned) {
      $listener = @(Get-NetTCPConnection -LocalPort 7100 -State Listen -ErrorAction Stop)[0]
      $script:agentProcess = Get-Process -Id $listener.OwningProcess -ErrorAction Stop
      if (Test-AgentHealth) { return }
      Stop-OwnedAgent
    }
    $launcherProcess = Start-Process -FilePath $Config.PythonExe -ArgumentList '-m','agent.run',"--keyword-agent-marker=$agentMarker" -WorkingDirectory (Join-Path $Config.ProjectRoot 'backend') -WindowStyle Hidden -PassThru
    for ($i=0; $i -lt 30; $i++) {
      if (Test-AgentHealth) {
        Assert-KeywordAgentPort $Config | Out-Null
        $listener = @(Get-NetTCPConnection -LocalPort 7100 -State Listen -ErrorAction Stop)[0]
        $script:agentProcess = Get-Process -Id $listener.OwningProcess -ErrorAction Stop
        Set-Content -LiteralPath (Join-Path $PSScriptRoot 'agent.pid') -Value $script:agentProcess.Id -Encoding ascii
        return
      }
      if ($launcherProcess.HasExited -and $launcherProcess.ExitCode -ne 0) { break }
      Start-Sleep 2
    }
    if (-not $launcherProcess.HasExited) { Stop-Process -Id $launcherProcess.Id -Force -ErrorAction SilentlyContinue }
    Stop-OwnedAgent
    throw 'Agent 在 60 秒内未通过本机 health，拒绝建立 SSH 隧道'
  }

  $delay = 2
  while ($true) {
    try {
      Start-OwnedAgent
      $reverse = "$($Config.ServerGateway):17100:127.0.0.1:7100"
      $commonSsh = @(Get-KeywordAgentSshBaseArgs -PrivateKey $privateKey -KnownHosts $knownHosts)
      $probeArgs = @($commonSsh) + @("keyword-agent-tunnel@$($Config.ServerHost)", "check-network $($Config.ServerNetworkId)")
      $networkProbe = Invoke-KeywordAgentSshProbe -Arguments $probeArgs -TimeoutMilliseconds 15000
      if ($networkProbe.Stdout -ne 'ok' -or (-not $networkProbe.TimedOut -and $networkProbe.ExitCode -ne 0)) {
        throw '服务器 Docker network 身份已变化，请重新生成隧道配置'
      }
      $sshArgs = @($commonSsh) + @('-N','-o','ExitOnForwardFailure=yes','-o','ServerAliveInterval=30','-o','ServerAliveCountMax=3','-R',$reverse,"keyword-agent-tunnel@$($Config.ServerHost)")
      $ownedTunnels = @(
        Get-CimInstance Win32_Process -Filter "Name='ssh.exe'" -ErrorAction SilentlyContinue |
          Where-Object { Test-KeywordAgentOwnedTunnelProcess -Process $_ -Config $Config -PrivateKey $privateKey -KnownHosts $knownHosts }
      )
      if ($ownedTunnels.Count -gt 1) { throw '发现多个关键词库 SSH 隧道，请先人工核对' }
      if ($ownedTunnels.Count -eq 1) {
        $sshProcess = Get-Process -Id $ownedTunnels[0].ProcessId -ErrorAction Stop
      } else {
        $sshProcess = Start-Process -FilePath 'ssh.exe' -ArgumentList $sshArgs -WindowStyle Hidden -PassThru
      }
      while (-not $sshProcess.HasExited) {
        if (-not (Test-AgentHealth)) {
          Stop-Process -Id $sshProcess.Id -Force -ErrorAction SilentlyContinue
          Stop-OwnedAgent
          break
        }
        Start-Sleep 5
      }
      Start-Sleep -Seconds $delay
      $delay = [Math]::Min($delay * 2, 60)
    } catch {
      Stop-OwnedAgent
      throw
    }
  }
} finally {
  if ($sshProcess -and -not $sshProcess.HasExited) { Stop-Process -Id $sshProcess.Id -Force -ErrorAction SilentlyContinue }
  Stop-OwnedAgent
  $mutex.ReleaseMutex()
  $mutex.Dispose()
}
