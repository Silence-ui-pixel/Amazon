import subprocess
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

ROOT = Path(__file__).resolve().parents[2]


class WindowsAgentDeploymentTests(TestCase):
    def test_ssh_probe_has_a_runtime_timeout_after_connecting(self) -> None:
        """A connected remote command must not be able to stall supervision forever."""

        module = ROOT / "deploy/windows-agent/KeywordAgent.psm1"
        command = (
            "$ErrorActionPreference='Stop'; "
            f"Import-Module -Force '{module}'; "
            "$watch=[Diagnostics.Stopwatch]::StartNew(); "
            "try { "
            "$pwsh=(Get-Command pwsh.exe -ErrorAction Stop).Source; "
            "Invoke-KeywordAgentSshProbe -Executable $pwsh "
            "-Arguments @('-NoProfile','-Command','Start-Sleep -Seconds 30') "
            "-TimeoutMilliseconds 200 | Out-Null; "
            "throw 'probe unexpectedly completed' "
            "} catch { "
            "$watch.Stop(); "
            "@{elapsed=$watch.Elapsed.TotalSeconds;message=$_.Exception.Message} "
            "| ConvertTo-Json -Compress "
            "}"
        )
        completed = subprocess.run(
            ["pwsh", "-NoProfile", "-Command", command],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=5,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        result = json.loads(completed.stdout)
        self.assertIn("timed out", result["message"].lower())
        self.assertLess(result["elapsed"], 3.0)

    def test_ssh_probe_accepts_complete_ok_before_transport_stalls(self) -> None:
        """A verified response remains usable after the stalled transport is killed."""

        module = ROOT / "deploy/windows-agent/KeywordAgent.psm1"
        command = (
            "$ErrorActionPreference='Stop'; "
            f"Import-Module -Force '{module}'; "
            "$watch=[Diagnostics.Stopwatch]::StartNew(); "
            "$pwsh=(Get-Command pwsh.exe -ErrorAction Stop).Source; "
            "$result=Invoke-KeywordAgentSshProbe -Executable $pwsh "
            "-Arguments @('-NoProfile','-Command',"
            "'Write-Output ok; Start-Sleep -Seconds 30') "
            "-TimeoutMilliseconds 1000; "
            "$watch.Stop(); "
            "@{elapsed=$watch.Elapsed.TotalSeconds;stdout=$result.Stdout;"
            "timed_out=$result.TimedOut} | ConvertTo-Json -Compress"
        )
        completed = subprocess.run(
            ["pwsh", "-NoProfile", "-Command", command],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=5,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        result = json.loads(completed.stdout)
        self.assertEqual(result["stdout"], "ok")
        self.assertTrue(result["timed_out"])
        self.assertLess(result["elapsed"], 4.0)

    def test_ssh_probe_is_bounded_and_owned_tunnel_is_adoptable(self) -> None:
        module = ROOT / "deploy/windows-agent/KeywordAgent.psm1"
        command = (
            "$ErrorActionPreference='Stop'; "
            f"Import-Module -Force '{module}'; "
            "$base=Get-KeywordAgentSshBaseArgs -PrivateKey 'C:\\agent\\id_ed25519' "
            "-KnownHosts 'C:\\agent\\known_hosts'; "
            "$config=@{ServerGateway='172.19.0.1';ServerHost='8.134.61.236'};"
            "$owned=[pscustomobject]@{CommandLine='ssh.exe -T -i C:\\agent\\id_ed25519 "
            "-o UserKnownHostsFile=C:\\agent\\known_hosts -N -R "
            "172.19.0.1:17100:127.0.0.1:7100 keyword-agent-tunnel@8.134.61.236'};"
            "$foreign=[pscustomobject]@{CommandLine='ssh.exe -N -R "
            "172.19.0.1:17100:127.0.0.1:9999 keyword-agent-tunnel@8.134.61.236'};"
            "@{args=$base;owned=(Test-KeywordAgentOwnedTunnelProcess -Process $owned "
            "-Config $config -PrivateKey 'C:\\agent\\id_ed25519' "
            "-KnownHosts 'C:\\agent\\known_hosts');"
            "foreign=(Test-KeywordAgentOwnedTunnelProcess -Process $foreign "
            "-Config $config -PrivateKey 'C:\\agent\\id_ed25519' "
            "-KnownHosts 'C:\\agent\\known_hosts')} | ConvertTo-Json -Depth 4 -Compress"
        )
        completed = subprocess.run(
            ["pwsh", "-NoProfile", "-Command", command],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        result = json.loads(completed.stdout)
        arguments = result["args"]
        self.assertIn("-T", arguments)
        self.assertIn("ConnectTimeout=15", arguments)
        self.assertIn("StrictHostKeyChecking=yes", arguments)
        self.assertIn("UserKnownHostsFile=C:\\agent\\known_hosts", arguments)
        self.assertIn("BatchMode=yes", arguments)
        self.assertTrue(result["owned"])
        self.assertFalse(result["foreign"])

    def test_installed_config_uses_the_copied_private_key(self) -> None:
        """A reboot must not depend on the installer's temporary source key."""

        module = ROOT / "deploy/windows-agent/KeywordAgent.psm1"
        python = ROOT / "backend/.venv/Scripts/python.exe"
        with TemporaryDirectory() as temporary:
            installed = Path(temporary)
            (installed / "id_ed25519").write_text("test-key", encoding="utf-8")
            missing_source = installed / "already-removed-source-key"
            command = (
                "$ErrorActionPreference='Stop'; "
                f"Import-Module -Force '{module}'; "
                "$config=@{"
                f"ProjectRoot='{ROOT}';PythonExe='{python}';ZiniaoCli='{module}';"
                "ServerHost='example.test';ServerGateway='172.19.0.1';"
                "ServerNetworkId='network-id';StoreId='26932126933854';"
                f"PrivateKey='{missing_source}';"
                "ServerSshHostKey='SHA256:test';ServerHostKeyLine='example.test ssh-ed25519 AAAA';"
                "AgentToken='0123456789abcdef0123456789abcdef'};"
                f"Assert-KeywordAgentConfig $config -InstalledRoot '{installed}'; 'ok'"
            )
            completed = subprocess.run(
                ["pwsh", "-NoProfile", "-Command", command],
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
            )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stdout.strip(), "ok")

    def test_venv_child_process_requires_the_project_marker(self) -> None:
        """Accept the venv base interpreter only when our launch marker is present."""

        module = ROOT / "deploy/windows-agent/KeywordAgent.psm1"
        python = ROOT / "backend/.venv/Scripts/python.exe"
        command = (
            "$ErrorActionPreference='Stop'; "
            f"Import-Module -Force '{module}'; "
            f"$config=@{{PythonExe='{python}'}}; "
            "$base=((Get-Content (Join-Path (Split-Path (Split-Path $config.PythonExe)) 'pyvenv.cfg')) "
            "| Where-Object {$_ -like 'executable = *'}).Substring(13).Trim(); "
            "$marker=Join-Path $env:LOCALAPPDATA 'KeywordLibraryAgent\\marker.json'; "
            "$owned=[pscustomobject]@{ExecutablePath=$base;CommandLine=\"$base -m agent.run --keyword-agent-marker=$marker\"}; "
            "$unmarked=[pscustomobject]@{ExecutablePath=$base;CommandLine=\"$base -m agent.run\"}; "
            "@{owned=(Test-KeywordAgentOwnedProcess -Config $config -Process $owned -Marker $marker);"
            "unmarked=(Test-KeywordAgentOwnedProcess -Config $config -Process $unmarked -Marker $marker)} "
            "| ConvertTo-Json -Compress"
        )
        completed = subprocess.run(
            ["pwsh", "-NoProfile", "-Command", command],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        result = json.loads(completed.stdout)
        self.assertTrue(result["owned"])
        self.assertFalse(result["unmarked"])

    def test_task_action_uses_an_existing_absolute_powershell_path(self) -> None:
        """Catch registering bare pwsh.exe, which Task Scheduler may not find."""

        module = ROOT / "deploy/windows-agent/KeywordAgent.psm1"
        command = (
            "$ErrorActionPreference='Stop'; "
            f"Import-Module -Force '{module}'; "
            "$action = Get-KeywordAgentTaskActionSpec "
            "-Target 'C:\\agent' -ProjectRoot 'C:\\project'; "
            "$action | ConvertTo-Json -Compress"
        )
        completed = subprocess.run(
            ["pwsh", "-NoProfile", "-Command", command],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        action = json.loads(completed.stdout)
        self.assertTrue(os.path.isabs(action["Execute"]), action)
        self.assertTrue(Path(action["Execute"]).is_file(), action)
        self.assertNotEqual(action["Execute"].lower(), "pwsh.exe")
        self.assertIn("-WindowStyle Hidden", action["Argument"])

    def test_supervisor_fixes_loopback_and_reverse_target(self) -> None:
        source = (ROOT / "deploy/windows-agent/run-supervisor.ps1").read_text(encoding="utf-8")
        self.assertIn("127.0.0.1:7100", source)
        self.assertIn("17100:127.0.0.1:7100", source)
        self.assertIn("ExitOnForwardFailure=yes", source)
        self.assertIn("ServerAliveInterval=30", source)
        self.assertIn("Get-KeywordAgentSshBaseArgs", source)
        self.assertIn("ServerSshHostKey", source)
        self.assertIn("System.Threading.Mutex", source)
        self.assertIn("while ($true)", source)
        self.assertIn("Test-AgentHealth", source)
        self.assertIn("Start-Process -FilePath 'ssh.exe'", source)
        self.assertIn("while (-not $sshProcess.HasExited)", source)
        self.assertNotIn("Write-Host $Config.AgentToken", source)

    def test_install_has_conflict_checks_and_whatif(self) -> None:
        source = (ROOT / "deploy/windows-agent/install.ps1").read_text(encoding="utf-8")
        module = (ROOT / "deploy/windows-agent/KeywordAgent.psm1").read_text(encoding="utf-8")
        self.assertIn("SupportsShouldProcess", source)
        self.assertIn("KeywordLibrary-Ziniao-Agent", source)
        self.assertIn("Assert-KeywordAgentPort", source)
        self.assertIn("Get-NetTCPConnection", module)
        self.assertIn("Win32_Process", module)
        self.assertIn("agent.run", module)
        self.assertIn("icacls.exe", source)
        self.assertIn("marker.json", source)
        self.assertIn("Register-ScheduledTask", source)

    def test_example_has_no_real_secret(self) -> None:
        source = (ROOT / "deploy/windows-agent/config.example.psd1").read_text(encoding="utf-8")
        self.assertIn("CHANGE_ME", source)
        self.assertNotIn("8.134.61.236", source)
        completed = subprocess.run(
            [
                "pwsh",
                "-NoProfile",
                "-Command",
                f"Import-PowerShellDataFile -LiteralPath '{ROOT / 'deploy/windows-agent/config.example.psd1'}' | Out-Null",
            ],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
