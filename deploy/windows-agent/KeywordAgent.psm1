function Assert-KeywordAgentConfig {
  param(
    [hashtable]$Config,
    [string]$InstalledRoot = ''
  )
  if ([string]::IsNullOrWhiteSpace($Config.AgentToken) -or $Config.AgentToken.Length -lt 32) { throw 'AgentToken 至少 32 字符' }
  foreach ($key in @('ProjectRoot','PythonExe','ServerHost','ServerGateway','ServerNetworkId','StoreId','PrivateKey','ServerSshHostKey','ServerHostKeyLine','ZiniaoCli')) {
    if ([string]::IsNullOrWhiteSpace($Config[$key]) -or $Config[$key] -like 'CHANGE_ME*') { throw "配置缺失：$key" }
  }
  foreach ($pathKey in @('ProjectRoot','PythonExe','ZiniaoCli')) {
    if (-not (Test-Path -LiteralPath $Config[$pathKey])) { throw "路径不存在：$pathKey" }
  }
  $privateKey = if ($InstalledRoot) { Join-Path $InstalledRoot 'id_ed25519' } else { $Config.PrivateKey }
  if (-not (Test-Path -LiteralPath $privateKey)) { throw '路径不存在：PrivateKey' }
}

function Test-KeywordAgentOwnedProcess {
  param(
    [hashtable]$Config,
    [object]$Process,
    [string]$Marker
  )
  $expectedPython = (Resolve-Path -LiteralPath $Config.PythonExe).Path
  $allowedExecutables = @($expectedPython)
  $venvConfig = Join-Path (Split-Path (Split-Path $expectedPython)) 'pyvenv.cfg'
  if (Test-Path -LiteralPath $venvConfig) {
    $baseLine = Get-Content -LiteralPath $venvConfig | Where-Object { $_ -match '^executable\s*=' } | Select-Object -First 1
    if ($baseLine) { $allowedExecutables += ($baseLine -split '=', 2)[1].Trim() }
  }
  $markerArgument = "--keyword-agent-marker=$Marker"
  return (
    $Process.ExecutablePath -in $allowedExecutables -and
    $Process.CommandLine.Contains('-m agent.run') -and
    $Process.CommandLine.Contains($markerArgument)
  )
}

function Assert-KeywordAgentPort {
  param([hashtable]$Config)
  $listeners = @(Get-NetTCPConnection -LocalPort 7100 -State Listen -ErrorAction SilentlyContinue)
  if (-not $listeners) { return $false }
  $marker = Join-Path $env:LOCALAPPDATA 'KeywordLibraryAgent\marker.json'
  if (-not (Test-Path -LiteralPath $marker)) { throw '7100 正在监听但 Agent marker 不存在' }
  $ownership = Get-Content -Raw -LiteralPath $marker | ConvertFrom-Json
  if ($ownership.marker -ne 'keyword-library-ziniao-agent:v1' -or $ownership.projectRoot -ne $Config.ProjectRoot) {
    throw '7100 正在监听但 Agent marker 归属不匹配'
  }
  foreach ($listener in $listeners) {
    if ($listener.LocalAddress -notin @('127.0.0.1','::1')) { throw '7100 被非 loopback 监听占用' }
    $process = Get-CimInstance Win32_Process -Filter "ProcessId=$($listener.OwningProcess)" -ErrorAction Stop
    if (-not (Test-KeywordAgentOwnedProcess -Config $Config -Process $process -Marker $marker)) {
      throw "7100 被未知进程占用（PID $($listener.OwningProcess)）"
    }
  }
  return $true
}

function Assert-KeywordAgentAcl {
  param([string]$Path)
  $acl = Get-Acl -LiteralPath $Path
  $unexpected = @($acl.Access | Where-Object {
    $_.IdentityReference.Value -notmatch "^(BUILTIN\\Administrators|NT AUTHORITY\\SYSTEM|$([regex]::Escape([System.Security.Principal.WindowsIdentity]::GetCurrent().Name)))$" -and
    $_.AccessControlType -eq 'Allow'
  })
  if ($unexpected) { throw "秘密文件 ACL 允许了其他账户：$Path" }
}

function Get-KeywordAgentTaskActionSpec {
  param(
    [Parameter(Mandatory=$true)][string]$Target,
    [Parameter(Mandatory=$true)][string]$ProjectRoot
  )
  # Task Scheduler does not inherit Codex/terminal PATH entries. Persist the
  # executable's absolute path so the task remains startable after logout.
  $powershell = (Get-Command pwsh.exe -ErrorAction Stop).Source
  if (-not [IO.Path]::IsPathFullyQualified($powershell) -or -not (Test-Path -LiteralPath $powershell)) {
    throw '无法解析 PowerShell 7 的绝对路径'
  }
  [pscustomobject]@{
    Execute = $powershell
    Argument = "-NoProfile -WindowStyle Hidden -File `"$Target\run-supervisor.ps1`" -ConfigPath `"$Target\config.psd1`""
    WorkingDirectory = Join-Path $ProjectRoot 'backend'
  }
}

function Get-KeywordAgentSshBaseArgs {
  param(
    [Parameter(Mandatory=$true)][string]$PrivateKey,
    [Parameter(Mandatory=$true)][string]$KnownHosts
  )
  @(
    '-T',
    '-i', $PrivateKey,
    '-o', 'BatchMode=yes',
    '-o', 'ConnectTimeout=15',
    '-o', 'StrictHostKeyChecking=yes',
    '-o', "UserKnownHostsFile=$KnownHosts",
    '-o', 'HostKeyAlgorithms=ssh-ed25519'
  )
}

function Invoke-KeywordAgentSshProbe {
  param(
    [string]$Executable = 'ssh.exe',
    [Parameter(Mandatory=$true)][string[]]$Arguments,
    [ValidateRange(1, 120000)][int]$TimeoutMilliseconds = 15000,
    [string]$ExpectedOutput = 'ok'
  )
  $startInfo = [Diagnostics.ProcessStartInfo]::new()
  $startInfo.FileName = $Executable
  $startInfo.UseShellExecute = $false
  $startInfo.CreateNoWindow = $true
  $startInfo.RedirectStandardOutput = $true
  $startInfo.RedirectStandardError = $true
  foreach ($argument in $Arguments) { $startInfo.ArgumentList.Add($argument) }

  $process = [Diagnostics.Process]::new()
  $process.StartInfo = $startInfo
  try {
    if (-not $process.Start()) { throw 'SSH probe failed to start' }
    $stdoutTask = $process.StandardOutput.ReadToEndAsync()
    $stderrTask = $process.StandardError.ReadToEndAsync()
    $timedOut = -not $process.WaitForExit($TimeoutMilliseconds)
    if ($timedOut) {
      try { $process.Kill($true) } catch { if (-not $process.HasExited) { throw } }
      $process.WaitForExit()
    }
    $stdout = $stdoutTask.GetAwaiter().GetResult().Trim()
    $stderr = $stderrTask.GetAwaiter().GetResult().Trim()
    if ($timedOut -and $stdout -ne $ExpectedOutput) {
      throw "SSH probe timed out after $TimeoutMilliseconds ms"
    }
    [pscustomobject]@{
      ExitCode = $process.ExitCode
      Stdout = $stdout
      Stderr = $stderr
      TimedOut = $timedOut
    }
  } finally {
    $process.Dispose()
  }
}

function Test-KeywordAgentOwnedTunnelProcess {
  param(
    [object]$Process,
    [hashtable]$Config,
    [string]$PrivateKey,
    [string]$KnownHosts
  )
  $command = [string]$Process.CommandLine
  $reverse = "$($Config.ServerGateway):17100:127.0.0.1:7100"
  $required = @(
    '-N',
    "-i $PrivateKey",
    "UserKnownHostsFile=$KnownHosts",
    "-R $reverse",
    "keyword-agent-tunnel@$($Config.ServerHost)"
  )
  foreach ($value in $required) {
    if ($command.IndexOf($value, [StringComparison]::OrdinalIgnoreCase) -lt 0) { return $false }
  }
  return $true
}

Export-ModuleMember -Function Assert-KeywordAgentConfig,Assert-KeywordAgentPort,Assert-KeywordAgentAcl,Get-KeywordAgentTaskActionSpec,Test-KeywordAgentOwnedProcess,Get-KeywordAgentSshBaseArgs,Invoke-KeywordAgentSshProbe,Test-KeywordAgentOwnedTunnelProcess
