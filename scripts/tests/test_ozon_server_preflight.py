from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
PREFLIGHT_PATH = REPOSITORY_ROOT / "deploy" / "ozon-server" / "preflight.py"
PREFLIGHT_SPEC = importlib.util.spec_from_file_location("ozon_server_preflight", PREFLIGHT_PATH)
if PREFLIGHT_SPEC is None or PREFLIGHT_SPEC.loader is None:
    raise RuntimeError(f"无法加载 {PREFLIGHT_PATH}")
PREFLIGHT_MODULE = importlib.util.module_from_spec(PREFLIGHT_SPEC)
PREFLIGHT_SPEC.loader.exec_module(PREFLIGHT_MODULE)
find_conflicts = PREFLIGHT_MODULE.find_conflicts


class OzonServerPreflightTests(TestCase):
    """部署必须在任何 Docker 变更之前拒绝他人占用的端口和同名资源。"""

    def test_rejects_port_listener_owned_by_another_container(self) -> None:
        conflicts = find_conflicts(
            project_dir="/home/kill_project/keyword_library",
            listeners={15433},
            published_containers={15433: {"other-container"}},
            project_containers={"our-container"},
            project_workdirs={"our-container": "/home/kill_project/keyword_library"},
            named_resource_owners={},
        )

        self.assertEqual(conflicts, ["端口 15433 已被非 keyword-library 项目占用"])

    def test_rejects_unmanaged_or_foreign_named_resources(self) -> None:
        conflicts = find_conflicts(
            project_dir="/home/kill_project/keyword_library",
            listeners=set(),
            published_containers={},
            project_containers=set(),
            project_workdirs={},
            named_resource_owners={
                "volume/keyword-library_pgdata": "<unmanaged>",
                "network/keyword-library_default": "another-project",
            },
        )

        self.assertEqual(
            conflicts,
            [
                "同名资源 volume/keyword-library_pgdata 不属于 keyword-library 项目",
                "同名资源 network/keyword-library_default 不属于 keyword-library 项目",
            ],
        )

    def test_allows_existing_resources_owned_by_this_project(self) -> None:
        project_dir = "/home/kill_project/keyword_library"
        conflicts = find_conflicts(
            project_dir=project_dir,
            listeners={15433, 18080},
            published_containers={15433: {"db"}, 18080: {"frontend"}},
            project_containers={"db", "frontend"},
            project_workdirs={"db": project_dir, "frontend": project_dir},
            named_resource_owners={
                "volume/keyword-library_pgdata": "keyword-library",
                "volume/keyword-library_backend_storage": "keyword-library",
                "network/keyword-library_default": "keyword-library",
            },
        )

        self.assertEqual(conflicts, [])

    def test_install_stops_when_preflight_fails_before_docker_mutation(self) -> None:
        bash = shutil.which("bash")
        if os.name == "nt":
            git_bash = Path(r"C:\Program Files\Git\bin\bash.exe")
            bash = str(git_bash) if git_bash.exists() else bash
        if not bash:
            self.skipTest("bash is required to execute the deployment script")

        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            project = root / "project"
            fake_bin = root / "fake-bin"
            secret = root / "dashscope"
            project.mkdir()
            fake_bin.mkdir()
            secret.mkdir()
            (secret / "api-key.csv").write_text("test-secret", encoding="utf-8")
            (project / ".env.production").write_text(
                "POSTGRES_PORT=15433\nHTTP_PORT=18080\n"
                f"DASHSCOPE_SECRET_DIR={self._bash_path(secret)}\n",
                encoding="utf-8",
            )
            (project / "deploy" / "ozon-server").mkdir(parents=True)
            docker_marker = root / "docker-called"
            python_stub = fake_bin / "python3"
            python_stub.write_text(
                "#!/usr/bin/env bash\necho 'preflight blocked' >&2\nexit 42\n",
                encoding="utf-8",
            )
            docker_stub = fake_bin / "docker"
            docker_stub.write_text(
                f"#!/usr/bin/env bash\nprintf called > '{self._bash_path(docker_marker)}'\nexit 0\n",
                encoding="utf-8",
            )
            for executable in ("chmod", "chown"):
                permission_stub = fake_bin / executable
                permission_stub.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
                permission_stub.chmod(0o755)
            python_stub.chmod(0o755)
            docker_stub.chmod(0o755)

            script = REPOSITORY_ROOT / "deploy" / "ozon-server" / "install.sh"
            command = (
                f'export PATH="{self._bash_path(fake_bin)}:$PATH"; '
                f'export KEYWORD_LIBRARY_PROJECT_DIR="{self._bash_path(project)}"; '
                f'bash "{self._bash_path(script)}"'
            )
            result = subprocess.run(
                [bash, "-lc", command],
                capture_output=True,
                text=True,
                encoding="utf-8",
                check=False,
            )

            self.assertEqual(result.returncode, 42, result.stderr)
            self.assertIn("preflight blocked", result.stderr)
            self.assertFalse(docker_marker.exists(), "Docker mutation ran before preflight")

    def test_install_grants_only_the_backend_secret_group_read_access(self) -> None:
        """Catch a root-only bind mount that the non-root backend cannot traverse."""

        bash = shutil.which("bash")
        if os.name == "nt":
            git_bash = Path(r"C:\Program Files\Git\bin\bash.exe")
            bash = str(git_bash) if git_bash.exists() else bash
        if not bash:
            self.skipTest("bash is required to execute the deployment script")

        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            project = root / "project"
            fake_bin = root / "fake-bin"
            secret = root / "dashscope"
            project.mkdir()
            fake_bin.mkdir()
            secret.mkdir()
            key = secret / "api-key.csv"
            key.write_text("test-secret", encoding="utf-8")
            (project / ".env.production").write_text(
                f"DASHSCOPE_SECRET_DIR={self._bash_path(secret)}\n",
                encoding="utf-8",
            )
            actions = root / "permission-actions"
            for executable in ("chmod", "chown"):
                stub = fake_bin / executable
                stub.write_text(
                    f"#!/usr/bin/env bash\nprintf '{executable} %s\\n' \"$*\" >> "
                    f"'{self._bash_path(actions)}'\n",
                    encoding="utf-8",
                )
                stub.chmod(0o755)
            python_stub = fake_bin / "python3"
            python_stub.write_text("#!/usr/bin/env bash\nexit 42\n", encoding="utf-8")
            python_stub.chmod(0o755)

            script = REPOSITORY_ROOT / "deploy" / "ozon-server" / "install.sh"
            command = (
                f'export PATH="{self._bash_path(fake_bin)}:$PATH"; '
                f'export KEYWORD_LIBRARY_PROJECT_DIR="{self._bash_path(project)}"; '
                "export DASHSCOPE_SECRET_GID=61001; "
                f'bash "{self._bash_path(script)}"'
            )
            result = subprocess.run(
                [bash, "-lc", command],
                capture_output=True,
                text=True,
                encoding="utf-8",
                check=False,
            )

            self.assertEqual(result.returncode, 42, result.stderr)
            recorded = actions.read_text(encoding="utf-8")
            self.assertIn(f"chown root:61001 {self._bash_path(secret)}", recorded)
            self.assertIn(f"chmod 0710 {self._bash_path(secret)}", recorded)
            self.assertIn(f"chown root:61001 {self._bash_path(key)}", recorded)
            self.assertIn(f"chmod 0640 {self._bash_path(key)}", recorded)

    @staticmethod
    def _bash_path(path: Path) -> str:
        resolved = path.resolve()
        if os.name != "nt":
            return resolved.as_posix()
        drive = resolved.drive.rstrip(":").lower()
        suffix = resolved.as_posix()[2:]
        return f"/{drive}{suffix}"
